from app.app.services.chunking import chunk_plain_text, chunk_transcript, estimate_tokens


def test_chunk_size_and_overlap():
    text = "Первое предложение. Второе предложение. Третье предложение."
    chunks = chunk_plain_text(text, max_tokens=8, overlap_tokens=2)
    assert len(chunks) >= 1
    assert all(c.token_estimate > 0 for c in chunks)


def test_chunks_do_not_split_sentences():
    text = "Alpha sentence. Beta sentence."
    chunks = chunk_plain_text(text, max_tokens=512)
    assert chunks[0].text.count(".") >= 1


def test_transcript_chunks_keep_speaker():
    text = "Спикер №1: первая реплика\nСпикер №1: вторая реплика"
    chunks = chunk_transcript(text)
    assert chunks[0].speaker == "Спикер №1"


def test_transcript_chunks_lab_timecodes_keep_names():
    """Окно реплик: вопрос и ответ «да» вместе, строки канона дословно (имя и таймкод видны), отрезок времени у чанка."""
    text = (
        "[00:00 - 02:01] Елена Соколова: Запись. Сегодня повестка.\n"
        "[02:01 - 02:02] Игорь (Пилот): Да, вас слышно.\n"
    )
    chunks = chunk_transcript(text)
    assert len(chunks) == 1
    body = chunks[0].text
    assert "[00:00 - 02:01] Елена Соколова: Запись." in body
    assert "[02:01 - 02:02] Игорь (Пилот): Да, вас слышно." in body
    assert chunks[0].speaker is None  # два говорящих в окне
    assert chunks[0].start_sec == 0.0 and chunks[0].end_sec == 122.0


def test_transcript_window_splits_and_overlaps():
    lines = [f"[{i:02d}:00 - {i:02d}:30] {'Анна' if i % 2 else 'Борис'}: " + "слово " * 20 for i in range(20)]
    chunks = chunk_transcript("\n".join(lines), max_tokens=200)
    assert len(chunks) > 1
    assert all(len(c.text) <= 200 * 4 for c in chunks)
    first_last = chunks[0].text.splitlines()[-1]
    assert chunks[1].text.splitlines()[0] == first_last  # перекрытие в одну реплику
    assert all(c.start_sec is not None for c in chunks)


def test_transcript_screen_blocks_go_to_own_windows():
    text = (
        "[00:10 - 00:20] Анна: Давайте смотреть.\n"
        "[00:21 - 00:25] Борис: Согласен.\n"
        "[75:10 - 75:20] Экран (показывал Анна): таблица номенклатуры\n"
    )
    chunks = chunk_transcript(text)
    assert len(chunks) == 2
    assert "Экран" not in chunks[0].text
    assert chunks[1].text.startswith("[75:10 - 75:20] Экран")
    assert chunks[1].start_sec == 75 * 60 + 10


def test_estimate_tokens_positive():
    assert estimate_tokens("hello world") >= 1


def test_index_artifact_prefers_lab_final(tmp_data, db_engine, client, admin_token, monkeypatch):
    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Chunk
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.indexing import index_artifact
    from app.app.services.storage import create_project_tree, parsed_dir

    dim = 1024
    monkeypatch.setattr(
        "app.app.services.indexing.embed_texts",
        lambda _s, texts: [[0.01] * dim for _ in texts],
    )
    monkeypatch.setattr("app.app.services.indexing.init_vector_store", lambda _e: None)
    monkeypatch.setattr("app.app.services.indexing.upsert_vectors", lambda _db, _pairs: None)
    monkeypatch.setattr("app.app.services.indexing.delete_vectors_for_chunks", lambda _db, _ids: None)

    settings = get_settings()
    db = SessionLocal()
    user = db.scalar(select(User).limit(1))
    project = Project(name="IdxLab", description="", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()
    db.refresh(project)
    create_project_tree(settings, project.id)
    art = Artifact(
        artifact_id="art_idx_lab",
        project_id=project.id,
        user_id=user.id,
        original_filename="call.webm",
        stored_filename="call.webm",
        file_path="x",
        file_type="audio",
        source_type="call",
        status="done",
        sha256="abc",
        transcription_text="STALE WHISPER MERGE",
    )
    db.add(art)
    db.commit()
    db.refresh(art)
    parsed_dir(settings, project.id, "call").mkdir(parents=True, exist_ok=True)
    (parsed_dir(settings, project.id, "call") / "art_idx_lab_final.txt").write_text(
        "[00:00 - 00:01] Елена Соколова: канон lab\n",
        encoding="utf-8",
    )
    n = index_artifact(db, settings, art, "STALE WHISPER MERGE")
    rows = list(db.scalars(select(Chunk).where(Chunk.artifact_id == art.id)).all())
    db.close()
    assert n >= 1
    assert any("канон lab" in (c.text or "") for c in rows)
    assert any(c.speaker == "Елена Соколова" for c in rows)
    assert rows[0].start_sec == 0.0 and rows[0].end_sec == 1.0


def test_audit_empty_project_is_ok(tmp_data, db_engine, client, admin_token):
    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.indexing import audit_project_index

    db = SessionLocal()
    user = db.scalar(select(User).limit(1))
    project = Project(name="EmptyIdx", description="", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()
    db.refresh(project)
    report = audit_project_index(db, get_settings(), project.id)
    db.close()
    assert report["ok"] is True
    assert report["chunks"] == 0
    assert report["artifacts"] == 0


SCREEN_FINAL = (
    "[00:10 - 00:20] Анна Петрова: Код базы храним в отдельном поле.\n"
    "[00:10 - 00:30] Экран (показывал Анна Петрова): " + "Окно браузера, таблица справочника. " * 40 + "\n"
    "[00:10 - 00:30] Речь на экране: Анна Петрова: Код Олег: базы храним Анна Петрова: в отдельном\n"
    "[00:31 - 00:40] Олег Смирнов: Согласен.\n"
)


def test_screen_speech_duplicate_not_indexed():
    chunks = chunk_transcript(SCREEN_FINAL)
    body = "\n".join(c.text for c in chunks)
    assert "Речь на экране" not in body
    assert "Код базы храним в отдельном поле." in body and "Окно браузера" in body


def test_extraction_parts_skip_screen_speech_and_trim_frames(tmp_data, monkeypatch):
    from types import SimpleNamespace

    from app.app.services import entities
    from app.app.services.transcript_turns import parse_final_turns

    monkeypatch.setattr("app.app.services.transcript_turns.load_turns", lambda _s, _a: parse_final_turns(SCREEN_FINAL))
    parts = entities._numbered_turn_parts(None, SimpleNamespace(source_type="call"))
    text = "\n".join(parts)
    assert "Речь на экране" not in text
    assert "T0 " in text and "T1 " in text and "T3 " in text and "T2 " not in text  # номера реплик не сдвинулись
    frame = next(line for line in text.splitlines() if line.startswith("T1 "))
    assert len(frame) < entities.SCREEN_LINE_EXTRACT_CHARS + 80 and frame.endswith("…")
