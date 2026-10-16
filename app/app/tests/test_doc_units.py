"""Адреса абзацев документа P<n> (docs/plans/DECISION-MEMORY-PLAN.md, Э1). Все тексты вымышленные."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.app.services import doc_units as du

DOC = """Протокол встречи

Заказчик: ООО «Пример»

# 1. Общие сведения

Таблица 1
| Параметр | Значение |
| --- | --- |
| Система | Учётная система УС-5 |
| Срок | до 15 октября |

Первый абзац первой строкой,
вторая строка того же абзаца.

## 2. Решения

* Использовать типовой механизм обмена.
* Старую номенклатуру не переносить.
1. Назначить ответственного.

Итоговый абзац без разметки.
"""


def test_split_units_kinds_texts_and_sequential_addresses():
    units = du.split_units(DOC)
    assert [u.id for u in units] == list(range(1, len(units) + 1))  # сквозная нумерация с единицы
    assert [(u.kind, u.text) for u in units] == [
        ("para", "Протокол встречи"),
        ("para", "Заказчик: ООО «Пример»"),
        ("heading", "1. Общие сведения"),
        ("row", "Параметр | Значение"),  # подпись «Таблица 1» и разделитель | --- | единицами не считаются
        ("row", "Система | Учётная система УС-5"),
        ("row", "Срок | до 15 октября"),
        ("para", "Первый абзац первой строкой, вторая строка того же абзаца."),  # строки абзаца склеены
        ("heading", "2. Решения"),
        ("list", "Использовать типовой механизм обмена."),
        ("list", "Старую номенклатуру не переносить."),
        ("list", "Назначить ответственного."),
        ("para", "Итоговый абзац без разметки."),
    ]
    assert units[0].address == "P1" and units[8].address == "P9"


def test_split_is_deterministic_and_offsets_point_into_text():
    first, second = du.split_units(DOC), du.split_units(DOC)
    assert first == second
    for u in first:
        assert u.text.split()[0] in DOC[u.start:u.end] or u.kind in ("row", "heading")  # смещения указывают на строки источника
        assert 0 <= u.start < u.end <= len(DOC)


def test_empty_and_whitespace_text():
    assert du.split_units("") == [] and du.split_units("   \n\n  ") == [] and du.split_units(None) == []


def test_numbered_lines_and_unit_lookup():
    units = du.split_units(DOC)
    lines = du.numbered_lines(units)
    assert lines[0] == "P1 Протокол встречи" and lines[4] == "P5 Система | Учётная система УС-5" and len(lines) == len(units)
    assert du.unit_by_id(units, 5).text.startswith("Система") and du.unit_by_id(units, 99) is None and du.unit_by_id(units, None) is None


def test_context_window_before_and_after():
    units = du.split_units(DOC)
    window = du.context_window(units, 6)
    assert [u.id for u in window] == [3, 4, 5, 6, 7]  # 3 до, источник, 1 после
    assert [u.id for u in du.context_window(units, 1)] == [1, 2]  # у начала документа окно короче
    assert [u.id for u in du.context_window(units, len(units))][-1] == len(units)
    assert du.context_window(units, 99) == []


def test_find_unit_by_quote_inside_spanning_and_preferred():
    units = du.split_units(DOC + "\nСрок | до 15 октября\n")
    assert du.find_unit_by_quote(units, "Старую номенклатуру НЕ переносить.").id == 10  # регистр и пробелы не важны
    assert du.find_unit_by_quote(units, "учётная  система УС-5").id == 5
    assert du.find_unit_by_quote(units, "коротко") is None and du.find_unit_by_quote(units, "") is None  # короче 12 знаков
    assert du.find_unit_by_quote(units, "такого текста в документе нет совсем") is None
    # цитата растянулась на две соседние единицы: берётся та, с которой она начинается
    assert du.find_unit_by_quote(units, "Использовать типовой механизм обмена. Старую номенклатуру не переносить.").id == 9
    twins = du.split_units("Срок до 15 октября.\n\nДругое.\n\nСрок до 15 октября.")
    assert du.find_unit_by_quote(twins, "Срок до 15 октября.").id == 1
    assert du.find_unit_by_quote(twins, "Срок до 15 октября.", prefer=[3]).id == 3  # подсказанная единица — первой


# ---------------------------------------------------------------- извлечение и привязка

def test_numbered_unit_parts_split_by_whole_units():
    from app.app.services import entities

    parts = entities._numbered_unit_parts(DOC, max_chars=80)
    assert len(parts) > 1 and all(line.startswith("P") for part in parts for line in part.splitlines())
    joined = [line for part in parts for line in part.splitlines()]
    assert joined == du.numbered_lines(du.split_units(DOC))  # ничего не потеряно и не повторено
    assert entities._numbered_unit_parts("") == [] and "P<номер>" in entities._paragraphs_header()


def test_schema_accepts_paragraph_numbers_in_any_form():
    from app.app.services.entities import DecisionItem, EntitiesPayload, TaskItem, _decision_value

    item = DecisionItem(text="Решение", paragraphs=["P12", 13, "14", None], quote="цитата")
    assert item.paragraphs == [12, 13, 14]
    assert _decision_value(item)["paragraphs"] == [12, 13, 14] and _decision_value(DecisionItem(text="x"))["text"] == "x"
    payload = EntitiesPayload.model_validate({"tasks": [{"description": "Задача", "paragraphs": "P7"}], "open_questions": [{"text": "Вопрос?", "paragraphs": [2]}]})
    assert payload.tasks[0].paragraphs == [7] and payload.open_questions[0].paragraphs == [2]
    assert TaskItem().paragraphs == []


def test_bind_document_source_cases():
    from app.app.services.entities import bind_document_source

    units = du.split_units(DOC)
    ok = bind_document_source({"text": "Не переносить старую номенклатуру", "quote": "Старую номенклатуру не переносить.", "paragraphs": [9]}, units, DOC)
    assert ok["source_paragraph"] == 10 and ok["source_quote"] == "Старую номенклатуру не переносить." and "quote" not in ok and "paragraphs" not in ok
    assert "source_status" not in ok

    hint = bind_document_source({"text": "Срок работ до 15 октября", "quote": "выдуманная цитата, которой нет", "paragraphs": [6]}, units, DOC)
    assert hint["source_paragraph"] == 6 and hint["source_basis"] == "hint" and hint["source_quote"] in DOC  # цитату выбрал код из единицы

    weak = bind_document_source({"text": "Закупить сервер и лицензии", "quote": "", "paragraphs": [6]}, units, DOC)
    assert weak["source_status"] == "quote_not_found" and "source_paragraph" not in weak  # слов пункта в единице нет

    nothing = bind_document_source({"description": "Пустой пункт", "quote": "", "paragraphs": [999]}, units, DOC)
    assert nothing["source_status"] == "quote_not_found"


def test_persist_entities_binds_document_items_to_paragraphs(db_engine, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.entities import EntitiesPayload, persist_entities

    settings = get_settings().model_copy(update={"dry_run": True})
    db = SessionLocal()
    try:
        user = User(name="docs-user", telegram_id="tg-docs", role="admin", password_hash="x")
        db.add(user)
        db.commit()
        project = Project(name="Документы", created_by=user.id, is_active=True)
        db.add(project)
        db.commit()
        # документ, загруженный как «созвон» (source_type=call, но не звук): адреса абзацев всё равно нужны
        art = Artifact(artifact_id="art_docunits", project_id=project.id, user_id=user.id, original_filename="протокол.docx", stored_filename="p",
                       file_path="/x", file_type="docx", source_type="call", sha256="docunits", status="done", transcription_text=DOC)
        db.add(art)
        db.commit()
        payload = EntitiesPayload(
            decisions=[{"text": "Старую номенклатуру не переносить", "quote": "Старую номенклатуру не переносить.", "paragraphs": ["P10"]}],
            tasks=[{"description": "Назначить ответственного", "quote": "", "paragraphs": [11]}],
        )
        persist_entities(db, settings, art, payload)
        db.commit()
        values = {e.entity_type: json.loads(e.value_json) for e in db.query(Entity).filter(Entity.artifact_id == art.id, Entity.entity_type.in_(("decision", "task"))).all()}
        assert values["decision"]["source_paragraph"] == 10 and values["decision"]["source_quote"] == "Старую номенклатуру не переносить."
        assert values["task"]["source_paragraph"] == 11 and values["task"]["source_basis"] == "hint"
        assert all("paragraphs" not in v and "quote" not in v for v in values.values())  # служебные поля модели не копятся в сущности
    finally:
        db.close()


# ---------------------------------------------------------------- привязка старых пунктов и карточка

@pytest.fixture()
def docs(db_engine, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    user = User(name="anchors-user", telegram_id="tg-anchors", role="admin", password_hash="x")
    db.add(user)
    db.commit()
    project = Project(name="Привязка", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()

    def art(aid, name, file_type, source_type, text):
        row = Artifact(artifact_id=aid, project_id=project.id, user_id=user.id, original_filename=name, stored_filename=aid, file_path="/x",
                       file_type=file_type, source_type=source_type, sha256=aid, status="done", transcription_text=text)
        db.add(row)
        db.commit()
        return row

    def item(a, kind, text, **extra):
        ent = Entity(artifact_id=a.id, entity_type=kind, value_json=json.dumps({"text" if kind == "decision" else "description": text, **extra}, ensure_ascii=False),
                     confidence=0.6, is_active=True, needs_review=True)
        db.add(ent)
        db.commit()
        return ent

    yield SimpleNamespace(db=db, settings=get_settings(), project=project, art=art, item=item)
    db.close()


def test_bind_project_binds_by_words_keeps_existing_and_skips_calls(docs):
    from app.app.services import doc_anchors

    doc = docs.art("art_a1", "документ.docx", "docx", "document", DOC)
    as_call = docs.art("art_a2", "загружен как созвон.docx", "docx", "call", DOC)  # документ, а не звук
    audio = docs.art("art_a3", "созвон.webm", "audio", "call", DOC)
    new = docs.item(doc, "decision", "Использовать типовой механизм обмена")
    weak = docs.item(doc, "task", "Закупить сервер и лицензии на следующей неделе")
    done = docs.item(doc, "decision", "Срок до 15 октября", source_paragraph=6, source_quote="уже было")
    quoted = docs.item(as_call, "decision", "что-то про номенклатуру", source_quote="Старую номенклатуру не переносить.")
    audio_item = docs.item(audio, "decision", "Использовать типовой механизм обмена")

    report = doc_anchors.bind_project(docs.db, docs.settings, docs.project.id, apply=False)
    assert report["total"] == {"items": 4, "already": 1, "bound": 2, "left": 1}  # звук в расчёт не входит
    assert json.loads(docs.db.get(type(new), new.id).value_json).get("source_paragraph") is None  # без apply ничего не записано

    doc_anchors.bind_project(docs.db, docs.settings, docs.project.id, apply=True)
    docs.db.expire_all()
    get = lambda e: json.loads(docs.db.get(type(e), e.id).value_json)  # noqa: E731
    assert get(new)["source_paragraph"] == 9 and get(new)["source_basis"] == "words" and get(new)["source_quote"] in DOC
    assert "source_paragraph" not in get(weak)
    assert get(done)["source_quote"] == "уже было"  # уже привязанное и прежняя цитата не тронуты
    assert get(quoted)["source_paragraph"] == 10  # адрес найден по проверенной цитате
    assert "source_paragraph" not in get(audio_item)
    again = doc_anchors.bind_project(docs.db, docs.settings, docs.project.id, apply=True)
    assert again["total"]["bound"] == 0 and again["total"]["already"] == 3  # повтор безопасен


def test_context_units_window_and_contract_evidence_paragraph(docs):
    from app.app.services import contract as ctr
    from app.app.services.requirement_review import _context_units

    doc = docs.art("art_ctx", "документ.docx", "docx", "document", DOC)
    bound = docs.item(doc, "decision", "Срок до 15 октября", source_paragraph=6, source_quote="Срок | до 15 октября")
    plain = docs.item(doc, "decision", "Без привязки")
    ctx = _context_units(docs.db, docs.settings, [bound.id, plain.id])
    assert set(ctx) == {bound.id}
    window = ctx[bound.id]
    assert [w["timecode"] for w in window] == ["абзац 3", "абзац 4", "абзац 5", "абзац 6", "абзац 7"]
    assert [w["source"] for w in window] == [False, False, False, True, False] and all(w["speaker"] is None for w in window)

    fake_ctx = SimpleNamespace(art=doc, occurred_at=None, chunks=[])
    evidence = ctr._quoted_document_evidence(json.loads(bound.value_json), fake_ctx)
    assert evidence["paragraph"] == 6 and evidence["quote"] == "Срок | до 15 октября" and "q=" in evidence["url"]


def test_scorecard_m9_counts_bound_active_document_items(docs):
    from app.app.services import memory_scorecard as sc

    doc = docs.art("art_m9", "документ.docx", "docx", "document", DOC)
    chat_like = docs.art("art_m9_audio", "звук.webm", "audio", "call", "")
    docs.item(doc, "decision", "Один", source_paragraph=2)
    docs.item(doc, "task", "Два")
    off = docs.item(doc, "decision", "Выключенный")
    off.is_active = False
    docs.db.commit()
    docs.item(chat_like, "decision", "Звуковой")
    value, bound, total = sc._m9(docs.db, docs.settings, docs.project.id)
    assert (bound, total) == (1, 2) and value == pytest.approx(0.5)
    other = docs.project.id + 100
    assert sc._m9(docs.db, docs.settings, other) == (None, 0, 0)


def test_captioned_table_rows_are_units_but_caption_like_paragraph_stays():
    """«Таблица 1: лист «Этапы»» над строками таблицы — подпись, строки — единицы (Excel, HTML, Word с подписью; до 2026-10-09 таблица
    целиком была одним абзацем). Абзац «Таблица 3. Перечень отчётов» без таблицы под ним остаётся абзацем."""
    from app.app.services.doc_units import split_units

    text = "Таблица 1: лист «Этапы»\n| Этап | Срок |\n| --- | --- |\n| Проектирование | 20.04.2026 |\n| Разработка | 20.10.2026 |\n\nТаблица 3. Перечень отчётов приведён ниже.\n"
    units = split_units(text)
    assert [(u.kind, u.text) for u in units] == [
        ("row", "Этап | Срок"), ("row", "Проектирование | 20.04.2026"), ("row", "Разработка | 20.10.2026"),
        ("para", "Таблица 3. Перечень отчётов приведён ниже.")]
