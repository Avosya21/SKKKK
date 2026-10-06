"""Речевые акты реплик (docs/plans/DECISION-MEMORY-PLAN.md, Э2). Модель подменена: проверяется код вокруг неё. Имена вымышленные."""
from __future__ import annotations

import json

import pytest

from app.app.services import speech_acts as sa
from app.app.services.speech_acts import DUnit


def _units(n=12):
    return [DUnit(f"M{i}", i, "Автор" if i % 2 else "Другой", f"Реплика номер {i} про срок и цену", f"2026-01-01T10:{i:02d}:00", None, f"2026-01-01 10:{i:02d}") for i in range(1, n + 1)]


def test_windows_split_targets_with_context_before():
    units = _units(20)
    wins = sa.windows(units)
    assert [len(t) for _c, t in wins] == [8, 8, 4]
    assert wins[0][0] == [] and [u.key for u in wins[1][0]] == [6, 7, 8]  # контекст — 3 реплики перед целевыми
    flat = [u.key for _c, t in wins for u in t]
    assert flat == list(range(1, 21))  # каждая реплика — целевая ровно один раз
    long = [DUnit(f"T{i}", i, "А", "x" * 2000) for i in range(6)]
    assert [len(t) for _c, t in sa.windows(long)] == [2, 2, 2]  # предел по знакам


def test_build_user_marks_context_and_targets():
    units = _units(5)
    text = sa.build_user(units[:2], units[2:4])
    lines = text.splitlines()[1:]
    assert [l[0] for l in lines] == ["·", "·", ">", ">"] and "M3 [2026-01-01 10:03] Автор:" in lines[2]


def test_parse_acts_validates_unit_act_and_verbatim_quote():
    targets = _units(3)
    raw = json.dumps({"acts": [
        {"unit": "M1", "act": "proposed", "quote": "Реплика номер 1 про срок", "about": "Срок"},
        {"unit": "m2", "act": "AGREED", "quote": "про срок и цену", "about": "x" * 400},  # регистр метки и вида не важен; about обрезается
        {"unit": "M3", "act": "придумано", "quote": "Реплика номер 3"},  # вида акта нет
        {"unit": "M99", "act": "info", "quote": "Реплика номер 1"},  # реплики нет среди целевых
        {"unit": "M3", "act": "info", "quote": "выдуманная цитата, которой нет"},  # цитаты нет в реплике
        {"unit": "M3", "act": "info", "quote": "ab"},  # слишком короткая
        {"unit": "M1", "act": "proposed", "quote": "реплика  НОМЕР 1 про срок"},  # дубль по смыслу
    ]}, ensure_ascii=False)
    acts, dropped = sa.parse_acts(raw, targets)
    assert [(a["unit"], a["act"]) for a in acts] == [("M1", "proposed"), ("M2", "agreed")]
    assert len(acts[1]["about"]) == sa.MAX_ABOUT
    assert dropped == {"unit": 1, "act": 1, "quote": 2, "limit": 0, "duplicate": 1}


def test_parse_acts_limits_per_unit_and_accepts_fenced_json_and_bare_list():
    targets = _units(1)
    four = [{"unit": "M1", "act": a, "quote": q} for a, q in (("proposed", "Реплика номер 1"), ("info", "про срок"), ("asked", "и цену"), ("done", "про срок и"))]
    acts, dropped = sa.parse_acts("```json\n" + json.dumps({"acts": four}, ensure_ascii=False) + "\n```", targets)
    assert len(acts) == sa.MAX_ACTS_PER_UNIT and dropped["limit"] == 1
    assert sa.parse_acts(json.dumps(four[:1], ensure_ascii=False), targets)[0][0]["act"] == "proposed"
    with pytest.raises(ValueError):
        sa.parse_acts('{"acts": "нет"}', targets)
    with pytest.raises(json.JSONDecodeError):
        sa.parse_acts("не json", targets)


# ---------------------------------------------------------------- материал целиком

@pytest.fixture()
def chat(db_engine, tmp_data, monkeypatch):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services import chat_export, people

    db = SessionLocal()
    user = User(name="acts-user", telegram_id="tg-acts", role="admin", password_hash="x")
    db.add(user)
    db.commit()
    project = Project(name="Акты", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()
    art = Artifact(artifact_id="art_acts", project_id=project.id, user_id=user.id, original_filename="чат.html", stored_filename="c", file_path="/x",
                   file_type="html", source_type="document", sha256="acts", status="done")
    db.add(art)
    db.commit()
    export = {"messages": [
        {"id": 1, "at": "2026-02-01T10:00:00", "author": "Ольга Заказчикова", "text": "Давайте сделаем отдельное приложение для менеджеров."},
        {"id": 2, "at": "2026-02-01T10:05:00", "author": "Павел Исполнителев", "text": "Согласен, стартуем в понедельник."},
        {"id": 3, "at": "2026-02-01T10:06:00", "author": "Павел Исполнителев", "text": "Спасибо!"},
    ]}
    monkeypatch.setattr(chat_export, "load_export", lambda _p: export)
    people.create_person(db, project.id, "Ольга Заказчикова", side="customer", side_source="human")
    db.commit()
    yield db, get_settings(), art
    db.close()


def _fake_ask(calls):
    def ask(system, user):
        calls.append(user)
        return json.dumps({"acts": [
            {"unit": "M1", "act": "proposed", "quote": "сделаем отдельное приложение", "about": "приложение для менеджеров"},
            {"unit": "M2", "act": "agreed", "quote": "Согласен", "about": "приложение"},
            {"unit": "M2", "act": "proposed", "quote": "стартуем в понедельник", "about": "старт работ"},
        ]}, ensure_ascii=False)
    return ask


def test_extract_artifact_stores_verified_acts_with_registry_side(chat):
    from app.app.models.speech_act import SpeechAct

    db, settings, art = chat
    calls: list[str] = []
    stats = sa.extract_artifact(db, settings, art, ask=_fake_ask(calls), review=False)
    assert stats["units"] == 3 and stats["windows"] == 1 and stats["acts"] == 3 and stats["errors"] == 0 and len(calls) == 1
    rows = {(r.unit_ref, r.act): r for r in db.query(SpeechAct).filter(SpeechAct.artifact_id == art.id).all()}
    assert set(rows) == {("M1", "proposed"), ("M2", "agreed"), ("M2", "proposed")}
    first = rows[("M1", "proposed")]
    assert first.author == "Ольга Заказчикова" and first.side == "customer" and first.person_id and first.said_at.startswith("2026-02-01T10:00")
    assert rows[("M2", "agreed")].side is None and rows[("M2", "agreed")].quote == "Согласен"  # человека нет в реестре: сторона неизвестна
    assert "M1 [2026-02-01 10:00] Ольга Заказчикова:" in calls[0]


def test_rerun_replaces_acts_and_only_refs_touches_selected(chat):
    from app.app.models.speech_act import SpeechAct

    db, settings, art = chat
    sa.extract_artifact(db, settings, art, ask=_fake_ask([]), review=False)
    sa.extract_artifact(db, settings, art, ask=_fake_ask([]), review=False)
    assert db.query(SpeechAct).filter(SpeechAct.artifact_id == art.id).count() == 3  # не копится

    def only_m2(system, user):
        assert "> M2" in user and "> M1" not in user  # целевой только M2
        return json.dumps({"acts": [{"unit": "M2", "act": "done", "quote": "стартуем", "about": "старт"}]}, ensure_ascii=False)

    sa.extract_artifact(db, settings, art, ask=only_m2, only_refs={"M2"}, review=False)
    acts = sorted((r.unit_ref, r.act) for r in db.query(SpeechAct).filter(SpeechAct.artifact_id == art.id).all())
    assert acts == [("M1", "proposed"), ("M2", "done")]  # M1 остался, M2 заменён


def test_invalid_answer_is_retried_then_counted_as_error(chat):
    db, settings, art = chat
    answers = iter(["не json", "опять не json"])
    stats = sa.extract_artifact(db, settings, art, ask=lambda s, u: next(answers), review=False)
    assert stats["errors"] == 1 and stats["acts"] == 0
    flaky = iter(["не json", json.dumps({"acts": [{"unit": "M1", "act": "info", "quote": "отдельное приложение"}]}, ensure_ascii=False)])
    again = sa.extract_artifact(db, settings, art, ask=lambda s, u: next(flaky), review=False)
    assert again["errors"] == 0 and again["acts"] == 1  # со второй попытки ответ принят


def test_document_without_speakers_has_no_units(chat, monkeypatch):
    from app.app.services import chat_export

    db, settings, art = chat
    monkeypatch.setattr(chat_export, "load_export", lambda _p: None)
    assert sa.dialog_units(settings, art) == []
    assert sa.extract_artifact(db, settings, art, ask=lambda s, u: "{}")["units"] == 0


def test_rerun_with_journal_events_does_not_break_on_foreign_keys(chat):
    from app.app.models.decision_memory import DecisionEvent, Subject
    from app.app.models.speech_act import SpeechAct
    from app.app.services import decision_log as dl

    db, settings, art = chat
    sa.extract_artifact(db, settings, art, ask=_fake_ask([]), review=False)
    subject = Subject(project_id=art.project_id, title="Приложение для менеджеров")
    db.add(subject)
    db.commit()
    for act in db.query(SpeechAct).filter(SpeechAct.artifact_id == art.id).all():
        dl.add_event(db, subject, "proposed", "2026-02-01T10:00:00", speech_act_id=act.id, decided_by="rule")
    first = db.query(SpeechAct).filter(SpeechAct.artifact_id == art.id).first()
    dl.add_event(db, subject, "agreed", "2026-02-02", precision="day", speech_act_id=first.id, decided_by="human")
    db.commit()
    sa.extract_artifact(db, settings, art, ask=_fake_ask([]), review=False)  # прежде падало: FOREIGN KEY constraint failed
    events = db.query(DecisionEvent).filter(DecisionEvent.subject_id == subject.id).all()
    assert [(e.decided_by, e.speech_act_id) for e in events] == [("human", None)]  # события правил ушли, решение человека осталось


def test_review_pass_fixes_kinds_and_keeps_draft_when_it_fails(chat):
    from app.app.models.speech_act import SpeechAct

    db, settings, art = chat
    prompts: list[str] = []

    def ask(system, user):
        prompts.append(user)
        if "Черновая разметка:" not in user:
            return json.dumps({"acts": [{"unit": "M2", "act": "proposed", "quote": "стартуем в понедельник", "about": "старт"}]}, ensure_ascii=False)
        return json.dumps({"acts": [{"unit": "M2", "act": "agreed", "quote": "Согласен", "about": "приложение"},
                                    {"unit": "M2", "act": "proposed", "quote": "не из текста", "about": "x"}]}, ensure_ascii=False)

    stats = sa.extract_artifact(db, settings, art, ask=ask)
    assert stats["reviewed"] == 1 and stats["changed"] == 1 and len(prompts) == 2 and "Черновая разметка:" in prompts[1]
    assert "stартуем" not in prompts[1] and '"act": "proposed"' in prompts[1]  # проверяющий видит черновик
    rows = sorted((r.unit_ref, r.act) for r in db.query(SpeechAct).filter(SpeechAct.artifact_id == art.id).all())
    assert rows == [("M2", "agreed")]  # цитата не из текста отброшена кодом

    def flaky(system, user):
        if "Черновая разметка:" in user:
            raise RuntimeError("модель недоступна")
        return json.dumps({"acts": [{"unit": "M2", "act": "proposed", "quote": "стартуем в понедельник", "about": "старт"}]}, ensure_ascii=False)

    stats = sa.extract_artifact(db, settings, art, ask=flaky)
    assert stats["review_errors"] == 1 and [r.act for r in db.query(SpeechAct).filter(SpeechAct.artifact_id == art.id).all()] == ["proposed"]
