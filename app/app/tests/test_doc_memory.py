"""Документы в памяти решений (docs/plans/DOCUMENT-MEMORY-PLAN.md): единицы, паспорт, акты документа, правило судьи. Модель подменена.
Имена и компании вымышленные."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.app.services import doc_memory as dm

DOC = """# Техническое задание

Исполнитель: ООО «Ромашка-Софт». Документ утверждаю: директор заказчика, 03.02.2026.

## Требования

Система должна загружать показания счётчиков ежедневно.

Таблица 1
| Задача | Статус |
| --- | --- |
| Загрузка тарифов | Готово |
| Перенос счётчиков | В работе [примечание: ждём заказчика] |
"""

PASSPORT = {"kind": "tz", "kind_quote": "Техническое задание", "author_side": "executor", "author_quote": "Исполнитель: ООО «Ромашка-Софт»",
            "approved": "unknown", "approved_by": "unknown", "approved_quote": "", "date": "2026-02-03", "date_quote": "03.02.2026"}


def test_document_units_use_sections_and_table_headers():
    units = dm.document_units(DOC, "Документ «ТЗ.docx»")
    texts = [u.text.strip() for u in units]
    assert "Техническое задание" not in texts and "Задача | Статус" not in texts  # заголовки и шапка таблицы — только контекст
    row = next(u for u in units if "Загрузка тарифов" in u.text)
    assert row.ref.startswith("P") and row.section == "Требования" and row.lead.startswith("таблица:") and "Статус" in row.lead
    assert all(u.author == "Документ «ТЗ.docx»" for u in units)


def test_parse_passport_keeps_only_verified_quotes():
    raw = json.dumps({**PASSPORT, "approved": "yes", "approved_by": "customer", "approved_quote": "Документ утверждаю: директор заказчика"},
                     ensure_ascii=False)
    p = dm.parse_passport(raw, DOC)
    assert (p["kind"], p["author_side"], p["approved"], p["approved_by"], p["date"]) == ("tz", "executor", "yes", "customer", "2026-02-03")
    fake = json.dumps({"kind": "contract", "author_side": "customer", "author_quote": "Заказчик: выдумано", "approved": "yes",
                       "approved_by": "both", "approved_quote": "подписи сторон", "date": "2026-01-01", "date_quote": "01.01.2026"}, ensure_ascii=False)
    q = dm.parse_passport(fake, DOC)
    assert (q["author_side"], q["approved"], q["approved_by"], q["date"]) == ("unknown", "unknown", "unknown", "")  # цитат нет в тексте


def test_doc_context_maps_approved_documented_to_customer_agreement():
    ctx = dm.DocContext(units=[], passport={"author_side": "executor", "approved": "yes", "approved_by": "customer"}, label="Д", said_at=None)
    assert ctx.act_and_side("documented") == ("agreed", "customer")
    assert ctx.act_and_side("done") == ("done", "executor")
    draft = dm.DocContext(units=[], passport={"author_side": "unknown", "approved": "no", "approved_by": "unknown"}, label="Д", said_at=None)
    assert draft.act_and_side("documented") == ("documented", None)
    assert "вид: документ" in draft.header()


def test_needs_judge_skips_single_document_without_conflicts():
    from app.app.services.subject_judge import needs_judge

    def a(ref, act, art=1):
        return SimpleNamespace(unit_ref=ref, act=act, artifact_id=art)

    assert not needs_judge([a("P1", "documented"), a("P7", "documented"), a("P9", "info")])  # один документ, одно и то же
    assert needs_judge([a("P1", "documented"), a("P5", "cancelled")])  # документ сам себе противоречит
    assert needs_judge([a("P1", "documented"), a("P2", "documented", art=2)])  # два документа
    assert needs_judge([a("P1", "documented"), a("T5", "agreed", art=3)])  # документ и созвон
    assert not needs_judge([a("P1", "info")])


def test_documented_is_rejected_for_dialog_units():
    from app.app.services import speech_acts as sa
    from app.app.services.speech_acts import DUnit

    targets = [DUnit("M1", 1, "А", "Записали требование в протокол"), DUnit("P2", 2, "Д", "Система должна работать")]
    raw = json.dumps({"acts": [{"unit": "M1", "act": "documented", "quote": "Записали требование"},
                               {"unit": "P2", "act": "documented", "quote": "Система должна работать"}]}, ensure_ascii=False)
    acts, dropped = sa.parse_acts(raw, targets)
    assert [a["unit"] for a in acts] == ["P2"] and dropped["act"] == 1


@pytest.fixture()
def doc(db_engine, tmp_data, monkeypatch):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services import chat_export

    db = SessionLocal()
    user = User(name="doc-mem", telegram_id="tg-doc-mem", role="admin", password_hash="x")
    db.add(user)
    db.commit()
    project = Project(name="Документы", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()
    art = Artifact(artifact_id="art_docmem", project_id=project.id, user_id=user.id, original_filename="ТЗ.docx", stored_filename="t", file_path="/x",
                   file_type="docx", source_type="document", sha256="docmem", status="done", transcription_text=DOC)
    db.add(art)
    db.commit()
    monkeypatch.setattr(chat_export, "load_export", lambda _p: None)
    yield db, get_settings(), art
    db.close()


def test_extract_artifact_marks_document_units_with_passport_side_and_date(doc):
    from app.app.models.speech_act import SpeechAct
    from app.app.services import speech_acts as sa

    db, settings, art = doc
    units = dm.document_units(DOC, dm.document_label(art))
    req = next(u for u in units if "показания" in u.text)
    row = next(u for u in units if "Загрузка тарифов" in u.text)
    acts = {"acts": [{"unit": req.ref, "act": "documented", "quote": "Система должна загружать показания", "about": "загрузка показаний"},
                     {"unit": row.ref, "act": "done", "quote": "Готово", "about": "загрузка тарифов"},
                     {"unit": row.ref, "act": "proposed", "quote": "выдумка", "about": "x"}]}
    passport_calls: list[str] = []
    window_calls: list[str] = []

    def ask(system, user):
        if "Ответь, что это за документ" in system:  # промпт паспорта
            passport_calls.append(user)
            return json.dumps(PASSPORT, ensure_ascii=False)
        window_calls.append(user)
        return json.dumps(acts, ensure_ascii=False)

    stats = sa.extract_artifact(db, settings, art, ask=ask)
    assert stats["document"]["author_side"] == "executor" and stats["acts"] == 2 and stats["dropped"]["quote"] == 1
    rows = db.query(SpeechAct).filter(SpeechAct.artifact_id == art.id).order_by(SpeechAct.unit_key).all()
    assert [(r.unit_ref, r.act, r.side, r.said_at, r.author) for r in rows] == [
        (req.ref, "documented", "executor", "2026-02-03", "Документ «ТЗ.docx»"), (row.ref, "done", "executor", "2026-02-03", "Документ «ТЗ.docx»")]
    assert rows[0].prompt_version == dm.ACTS_PROMPT_VERSION
    assert any("[таблица:" in u for u in window_calls) and any("§ Требования" in u for u in window_calls)
    assert len(passport_calls) == 1
    sa.extract_artifact(db, settings, art, ask=ask)
    assert len(passport_calls) == 1  # паспорт второй раз не спрашивается: текст тот же, лежит в файле
    assert db.query(SpeechAct).filter(SpeechAct.artifact_id == art.id).count() == 2  # повторный разбор заменяет акты, а не дописывает


def test_compare_finds_acts_by_document_paragraph():
    from app.app.services.memory_compare import _acts_for

    acts = [SimpleNamespace(artifact_id=5, unit_ref="P3", start_sec=None), SimpleNamespace(artifact_id=5, unit_ref="P4", start_sec=None),
            SimpleNamespace(artifact_id=6, unit_ref="P3", start_sec=None)]
    assert _acts_for(acts, 5, {"source_paragraph": 3}) == [acts[0]]
    assert _acts_for(acts, 5, {"text": "без адреса"}) == []


def test_agent_exchange_files_are_not_documents():
    assert dm.is_document(SimpleNamespace(file_type="docx", original_filename="ТЗ.docx"))
    assert not dm.is_document(SimpleNamespace(file_type="txt", original_filename="analysis_question_3.json"))
    assert not dm.is_document(SimpleNamespace(file_type="audio", original_filename="встреча.wav"))
