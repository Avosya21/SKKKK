"""Имена опознанных голосов доходят до памяти решений (``services/act_authors.py``). Модель подменена. Имена вымышленные."""
from __future__ import annotations

import json

import pytest

from app.app.services import speech_acts as sa

UNNAMED = (
    "[00:10 - 00:20] speaker_0: Предлагаю вынести длину листа в реквизит заказа.\n"
    "[00:21 - 00:21] speaker_1: Да, согласен, так и делаем.\n"
    "[00:21 - 00:30] speaker_0: Тогда оценим склад примерно в сто часов.\n"
)


def _write_final(settings, art, text):
    from app.app.services.storage import parsed_dir

    path = parsed_dir(settings, art.project_id, art.source_type)
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{art.artifact_id}_final.txt").write_text(text, encoding="utf-8")


def _ask(system, user):
    return json.dumps({"acts": [
        {"unit": "T0", "act": "proposed", "quote": "вынести длину листа в реквизит", "about": "реквизит заказа"},
        {"unit": "T1", "act": "agreed", "quote": "согласен, так и делаем", "about": "реквизит заказа"},
        {"unit": "T2", "act": "proposed", "quote": "оценим склад примерно в сто часов", "about": "оценка склада"},
    ]}, ensure_ascii=False)


@pytest.fixture()
def call(db_engine, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services import people

    db = SessionLocal()
    user = User(name="authors-user", telegram_id="tg-authors", role="admin", password_hash="x")
    db.add(user)
    db.commit()
    project = Project(name="Авторы актов", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()
    art = Artifact(artifact_id="art_authors", project_id=project.id, user_id=user.id, original_filename="созвон.wav",
                   stored_filename="созвон.wav", file_path="созвон.wav", file_type="audio", source_type="call", sha256="b" * 64,
                   status="done")
    db.add(art)
    db.commit()
    settings = get_settings()
    _write_final(settings, art, UNNAMED)
    people.create_person(db, project.id, "Ольга Примерова", side="customer", side_source="human")
    people.create_person(db, project.id, "Павел Тестов", side="executor", side_source="human")
    db.commit()
    yield db, settings, art
    db.close()


def _memory(db, settings, art):
    """Акты по стенограмме с метками голосов и события журнала из них — как строит память решений."""
    from app.app.models.decision_memory import Subject
    from app.app.models.speech_act import SpeechAct
    from app.app.services import decision_log as dl

    sa.extract_artifact(db, settings, art, ask=_ask, review=False)
    subject = Subject(project_id=art.project_id, title="Реквизит заказа")
    db.add(subject)
    db.commit()
    acts = {a.unit_ref: a for a in db.query(SpeechAct).filter(SpeechAct.artifact_id == art.id).all()}
    for ref, kind in (("T0", "proposed"), ("T1", "agreed"), ("T2", "proposed")):
        a = acts[ref]
        dl.add_event(db, subject, kind, "2026-02-01T10:00:00", actor_label=a.author, actor_person_id=a.person_id,
                     actor_side=dl.normalize_side(a.side), speech_act_id=a.id, decided_by="rule")
    db.commit()
    return subject, acts


def test_voices_named_after_memory_reach_acts_and_events(call):
    """Голоса опознали после построения памяти: подтверждение имён (``rebind_call_entities``) переносит имена в акты и
    события; реплики с одной секундой начала не путаются; повторный проход ничего не меняет."""
    from app.app.models.decision_memory import DecisionEvent
    from app.app.services.act_authors import rebind_call_authors
    from app.app.services.entities import rebind_call_entities

    db, settings, art = call
    subject, acts = _memory(db, settings, art)
    assert {a.author for a in acts.values()} == {"speaker_0", "speaker_1"} and all(a.person_id is None for a in acts.values())

    _write_final(settings, art, UNNAMED.replace("speaker_0", "Ольга Примерова").replace("speaker_1", "Павел Тестов"))
    rebind_call_entities(db, settings, art)
    db.commit()
    db.expire_all()
    got = {ref: (a.author, a.side) for ref, a in ((r, db.get(type(a), a.id)) for r, a in acts.items())}
    assert got == {"T0": ("Ольга Примерова", "customer"), "T1": ("Павел Тестов", "executor"), "T2": ("Ольга Примерова", "customer")}
    events = {e.speech_act_id: e for e in db.query(DecisionEvent).filter(DecisionEvent.subject_id == subject.id).all()}
    olga = events[acts["T0"].id]
    assert (olga.actor_label, olga.actor_side) == ("Ольга Примерова", "customer") and olga.actor_person_id == acts["T0"].person_id
    assert events[acts["T1"].id].actor_label == "Павел Тестов" and events[acts["T1"].id].actor_person_id is not None
    assert rebind_call_authors(db, settings, art) == {"acts": 0, "people": 0, "events": 0, "subjects": 0}


def test_refined_voice_name_keeps_person(call):
    """Имя голоса уточнили («… (отдел)»), а такого написания в реестре нет — у акта остаётся прежний человек."""
    from app.app.models.speech_act import SpeechAct
    from app.app.services.act_authors import rebind_call_authors

    db, settings, art = call
    _write_final(settings, art, UNNAMED.replace("speaker_0", "Ольга Примерова").replace("speaker_1", "Павел Тестов"))
    _, acts = _memory(db, settings, art)
    olga_id = acts["T0"].person_id
    assert olga_id is not None

    _write_final(settings, art, UNNAMED.replace("speaker_0", "Ольга Примерова (отдел)").replace("speaker_1", "Павел Тестов"))
    stats = rebind_call_authors(db, settings, art)
    db.commit()
    db.expire_all()
    assert stats["acts"] == 2
    t0 = db.get(SpeechAct, acts["T0"].id)
    assert (t0.author, t0.person_id, t0.side) == ("Ольга Примерова (отдел)", olga_id, "customer")


def test_judge_verdict_survives_rebind(call):
    """Имена голосов пришли в акты после вердикта судьи: события получают людей, а итог судьи не заменяется свёрткой."""
    from app.app.models.decision_memory import Subject
    from app.app.services.act_authors import rebind_call_authors

    db, settings, art = call
    subject, _ = _memory(db, settings, art)
    subject.state, subject.state_since, subject.state_source = "rejected", "2026-02-01", "judge"
    db.commit()

    _write_final(settings, art, UNNAMED.replace("speaker_0", "Ольга Примерова").replace("speaker_1", "Павел Тестов"))
    stats = rebind_call_authors(db, settings, art)
    db.commit()
    db.expire_all()
    assert stats["events"] == 3 and stats["subjects"] == 1
    s = db.get(Subject, subject.id)
    assert (s.state, s.state_since, s.state_source) == ("rejected", "2026-02-01", "judge")
