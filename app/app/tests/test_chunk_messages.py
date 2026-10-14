"""Переписка в индексе по сообщениям (PORTAL-ACCURACY-MANAGER-PLAN, Ш2): нарезка, номера M…, повторы выгрузок, ссылки."""
from __future__ import annotations

from app.app.services.chunking import CHARS_PER_TOKEN, chunk_messages
from app.app.services.source_links import unit_number


def _msg(i: int, at: str, author: str, text: str, reply_to: int | None = None, **extra) -> dict:
    return {"id": i, "at": at, "author": author, "text": text, "reply_to": reply_to, **extra}


def _export(messages: list[dict], title: str = "Внедрение: рабочий чат") -> dict:
    return {"title": title, "messages": messages}


def test_chunk_has_day_header_numbers_and_lines():
    ex = _export([
        _msg(1, "2026-08-25T15:58:22+03:00", "Анна Петрова", "Когда пришлёте выгрузку?"),
        _msg(2, "2026-08-25T16:01:00+03:00", "Олег Смирнов", "Завтра до обеда.", reply_to=1),
    ])
    [ch] = chunk_messages(ex)
    lines = ch.text.splitlines()
    assert lines[0] == "Переписка «Внедрение: рабочий чат» · 25 августа 2026"
    assert lines[1] == "**15:58 Анна Петрова:** Когда пришлёте выгрузку?"
    assert lines[2].startswith("**16:01 Олег Смирнов:** (в ответ на M1, Анна Петрова: «Когда пришлёте выгрузку?») Завтра")
    assert (ch.unit_first, ch.unit_last) == ("M1", "M2")
    assert ch.speaker is None  # два автора


def test_new_day_starts_new_chunk_and_single_author_is_speaker():
    ex = _export([
        _msg(1, "2026-08-25T10:00:00+03:00", "Анна Петрова", "Первый день."),
        _msg(2, "2026-08-26T10:00:00+03:00", "Анна Петрова", "Второй день."),
    ])
    a, b = chunk_messages(ex)
    assert "25 августа" in a.text and "26 августа" in b.text
    assert a.speaker == b.speaker == "Анна Петрова"
    assert (a.unit_first, b.unit_first) == ("M1", "M2")


def test_long_pause_cuts_only_a_full_chunk():
    body = "слово " * 180  # ~1080 знаков: после переноса в новый фрагмент он уже наполовину полон
    near = [_msg(i, f"2026-08-25T10:{i:02d}:00+03:00", "Анна Петрова", body) for i in range(1, 4)]
    late = _msg(9, "2026-08-25T15:00:00+03:00", "Олег Смирнов", "Вечером вернёмся к вопросу.")
    chunks = chunk_messages(_export(near + [late]), max_tokens=512)
    assert chunks[-1].unit_first == "M9"  # фрагмент наполовину полон — пауза режет
    sparse = [_msg(1, "2026-08-25T10:00:00+03:00", "Анна Петрова", "Коротко."), late]
    assert len(chunk_messages(_export(sparse))) == 1  # редкая переписка не распадается на одиночные сообщения


def test_long_message_is_split_with_prefix_and_number():
    text = " ".join(f"Предложение номер {i} про обмен и номенклатуру." for i in range(200))
    chunks = chunk_messages(_export([_msg(5, "2026-08-25T10:00:00+03:00", "Анна Петрова", text)]), max_tokens=128)
    assert len(chunks) > 1
    assert all(c.unit_first == c.unit_last == "M5" for c in chunks)
    assert all("**10:00 Анна Петрова:**" in c.text for c in chunks)
    assert all(len(c.text) <= 128 * CHARS_PER_TOKEN + 80 for c in chunks)


def test_size_cut_repeats_last_message():
    msgs = [_msg(i, f"2026-08-25T10:{i:02d}:00+03:00", "Анна Петрова" if i % 2 else "Олег Смирнов", f"Сообщение {i} " + "текст " * 30)
            for i in range(1, 20)]
    chunks = chunk_messages(_export(msgs), max_tokens=256)
    assert len(chunks) > 1
    for a, b in zip(chunks, chunks[1:]):
        assert b.unit_first == a.unit_last  # вопрос и ответ не теряют друг друга


def test_bitrix_copy_without_time_and_skip_ids():
    msgs = [_msg(1, "2026-09-28T00:00:00+03:00", "Анна Петрова", "Опоздания считаем с 9:15.", at_precision="day"),
            _msg(2, "2026-09-28T00:00:00+03:00", "Олег Смирнов", "Принято.", at_precision="day")]
    [ch] = chunk_messages(_export(msgs, title=""))
    assert ch.text.splitlines()[0] == "Переписка · 28 сентября 2026"
    assert "**Анна Петрова:** Опоздания" in ch.text and "00:00" not in ch.text
    [only] = chunk_messages(_export(msgs), skip_ids={1})
    assert only.unit_first == "M2" and "Опоздания" not in only.text
    assert chunk_messages(_export(msgs), skip_ids={1, 2}) == []


def test_unit_number():
    assert unit_number("M105", "M") == 105
    assert unit_number("P12", "M") is None
    assert unit_number(None, "M") is None and unit_number("Mx", "M") is None


def _stub_vectors(monkeypatch):
    monkeypatch.setattr("app.app.services.indexing.embed_texts", lambda _s, texts: [[0.01] * 1024 for _ in texts])
    monkeypatch.setattr("app.app.services.indexing.init_vector_store", lambda _e: None)
    monkeypatch.setattr("app.app.services.indexing.upsert_vectors", lambda _db, _pairs: None)
    monkeypatch.setattr("app.app.services.indexing.delete_vectors_for_chunks", lambda _db, _ids: None)


def test_index_overlapping_exports_and_siblings(tmp_data, db_engine, client, admin_token, monkeypatch):
    """Две выгрузки одной переписки: сообщения ранней — только в её индексе (Я1); без разметки — прежний путь."""
    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Chunk
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.chat_export import artifact_messages_path, save_export
    from app.app.services.indexing import chat_export_siblings, index_artifact
    from app.app.services.storage import create_project_tree

    _stub_vectors(monkeypatch)
    settings = get_settings()
    db = SessionLocal()
    user = db.scalar(select(User).limit(1))
    project = Project(name="IdxChat", description="", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()
    create_project_tree(settings, project.id)

    def art(aid: str) -> Artifact:
        a = Artifact(artifact_id=aid, project_id=project.id, user_id=user.id, original_filename=f"{aid}.html", stored_filename=f"{aid}.html",
                     file_path="x", file_type="html", source_type="document", status="done", sha256=aid, transcription_text="")
        db.add(a)
        db.commit()
        return a

    early, late, plain = art("art_chat_early"), art("art_chat_late"), art("art_plain_doc")
    m1 = _msg(1, "2026-08-25T10:00:00+03:00", "Анна Петрова", "Старое сообщение про склад.")
    m2 = _msg(2, "2026-08-26T10:00:00+03:00", "Олег Смирнов", "Новое сообщение про цены.")
    save_export(artifact_messages_path(settings, early), _export([m1]), artifact_id=early.artifact_id, uploaded_at=None)
    from datetime import datetime

    save_export(artifact_messages_path(settings, late), _export([m1, m2]), artifact_id=late.artifact_id, uploaded_at=datetime(2026, 9, 1))
    save_export(artifact_messages_path(settings, early), _export([m1]), artifact_id=early.artifact_id, uploaded_at=datetime(2026, 8, 27))

    assert index_artifact(db, settings, early, "") == 1
    assert index_artifact(db, settings, late, "") == 1
    late_rows = db.scalars(select(Chunk).where(Chunk.artifact_id == late.id)).all()
    assert [c.unit_first for c in late_rows] == ["M2"] and "склад" not in late_rows[0].text
    assert index_artifact(db, settings, plain, "Обычный документ без переписки. Второе предложение.") >= 1
    assert all(c.unit_first is None for c in db.scalars(select(Chunk).where(Chunk.artifact_id == plain.id)).all())
    assert chat_export_siblings(db, settings, early) == ["art_chat_late"]
    assert chat_export_siblings(db, settings, plain) == []
    db.close()
