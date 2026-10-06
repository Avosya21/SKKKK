"""Предметы обсуждения (docs/plans/DECISION-MEMORY-PLAN.md, Э3). Векторы и модель подменены; имена вымышленные."""
from __future__ import annotations

import json

import pytest

from app.app.services import subjects as sj


def test_pack_unpack_cosine():
    vec = [0.25, -0.5, 1.0]
    assert sj.unpack(sj.pack(vec)) == vec and sj.unpack(None) == [] and sj.unpack(b"") == []
    assert sj.cosine([1, 0], [1, 0]) == pytest.approx(1) and sj.cosine([1, 0], [0, 1]) == 0 and sj.cosine([], [1]) == 0 and sj.cosine([0, 0], [1, 0]) == 0


def test_parse_choice_validates_candidate_number():
    assert sj.parse_choice('{"same": 2, "title": null}', 3) == (1, None)
    assert sj.parse_choice('{"same": "1"}', 3) == (0, None)
    assert sj.parse_choice('{"same": null, "title": "Новый вопрос"}', 3) == (None, "Новый вопрос")
    assert sj.parse_choice('{"same": 7, "title": "Вне списка"}', 3) == (None, "Вне списка")  # номер не из предложенных — новый предмет
    assert sj.parse_choice('{"same": true}', 3) == (None, None)
    assert sj.parse_choice('```json\n{"same": 1}\n```', 2) == (0, None)
    with pytest.raises(ValueError):
        sj.parse_choice("не json", 2)


@pytest.fixture()
def world(db_engine, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User

    db = SessionLocal()
    user = User(name="subj-user", telegram_id="tg-subj", role="admin", password_hash="x")
    db.add(user)
    db.commit()
    project = Project(name="Предметы", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()
    art = Artifact(artifact_id="art_subj", project_id=project.id, user_id=user.id, original_filename="чат.html", stored_filename="c", file_path="/x",
                   file_type="html", source_type="document", sha256="subj", status="done")
    db.add(art)
    db.commit()
    yield db, get_settings(), project, art
    db.close()


def _acts(db, project, art, rows):
    from app.app.models.speech_act import SpeechAct

    made = []
    for i, (about, quote, act, said) in enumerate(rows, 1):
        row = SpeechAct(project_id=project.id, artifact_id=art.id, unit_ref=f"M{i}", unit_key=i, author="А", act=act, quote=quote, about=about, said_at=said,
                        prompt_version="v", model="m")
        db.add(row)
        made.append(row)
    db.commit()
    return made


# Вектор по смыслу: ось 0 — «приложение», ось 1 — «цена», ось 2 — «пометка»; 0.7-ось — «похоже, но не точно»
AXES = {"приложение": [1.0, 0.0, 0.0], "цена": [0.0, 1.0, 0.0], "пометка": [0.0, 0.0, 1.0], "пограничное": [0.7, 0.0, 0.7], "иное": [0.6, 0.6, 0.0]}


def fake_embed(texts):
    out = []
    for t in texts:
        low = t.lower()
        out.append(next((v for k, v in AXES.items() if k in low), [0.3, 0.3, 0.3]))
    return out


def test_assign_groups_by_similarity_and_creates_new_subjects(world):
    from app.app.models.decision_memory import Subject
    from app.app.models.speech_act import SpeechAct

    db, settings, project, art = world
    rows = [("приложение для менеджеров", "приложение предложили", "proposed", "2026-01-01T10:00"),
            ("цена работ", "цена 126 000", "info", "2026-01-01T10:05"),
            ("приложение для менеджеров", "согласны на приложение", "agreed", "2026-01-02T10:00"),
            ("пометка старой номенклатуры", "пометка восклицательным знаком", "proposed", "2026-01-03T10:00")]
    _acts(db, project, art, rows)
    asked: list[str] = []
    stats = sj.assign(db, settings, project.id, embed=fake_embed, ask=lambda s, u: asked.append(u) or "{}")
    assert stats == {"acts": 4, "attached": 1, "created": 3, "asked": 0, "ask_errors": 0, "subjects": 3} and asked == []
    by_ref = {a.unit_ref: a.subject_id for a in db.query(SpeechAct).all()}
    assert by_ref["M1"] == by_ref["M3"] and len({by_ref["M1"], by_ref["M2"], by_ref["M4"]}) == 3
    titles = {s.id: s.title for s in db.query(Subject).all()}
    assert titles[by_ref["M1"]] == "приложение для менеджеров" and sj.unpack(db.get(Subject, by_ref["M1"]).embedding) == [1.0, 0.0, 0.0]


def test_borderline_similarity_asks_model_and_obeys_the_answer(world):
    from app.app.models.speech_act import SpeechAct

    db, settings, project, art = world
    _acts(db, project, art, [("приложение для менеджеров", "приложение", "proposed", "2026-01-01T10:00"),
                             ("пограничное: про менеджеров", "про менеджеров", "agreed", "2026-01-01T11:00"),
                             ("иное: другой вопрос", "совсем иное", "proposed", "2026-01-01T12:00")])
    answers = iter(['{"same": 1}', '{"same": null, "title": "Другое решение"}'])
    questions: list[str] = []
    stats = sj.assign(db, settings, project.id, embed=fake_embed, ask=lambda s, u: questions.append(u) or next(answers))
    assert stats["asked"] == 2 and stats["attached"] == 1 and stats["created"] == 2
    acts = {a.unit_ref: a.subject_id for a in db.query(SpeechAct).all()}
    assert acts["M2"] == acts["M1"] and acts["M3"] not in (acts["M1"], None)
    assert "Кандидаты:" in questions[0] and "1. приложение для менеджеров" in questions[0] and "Новая реплика" in questions[0]
    from app.app.models.decision_memory import Subject

    assert db.get(Subject, acts["M3"]).title == "Другое решение"


def test_model_failure_creates_new_subject_instead_of_merging(world):
    db, settings, project, art = world
    _acts(db, project, art, [("приложение", "приложение", "proposed", "2026-01-01T10:00"), ("пограничное", "что-то", "agreed", "2026-01-01T11:00")])

    def broken(system, user):
        raise RuntimeError("модель недоступна")

    stats = sj.assign(db, settings, project.id, embed=fake_embed, ask=broken)
    assert stats["asked"] == 1 and stats["ask_errors"] == 1 and stats["created"] == 2 and stats["attached"] == 0


def test_incremental_assign_attaches_new_acts_to_existing_subjects(world):
    from app.app.models.speech_act import SpeechAct

    db, settings, project, art = world
    _acts(db, project, art, [("приложение", "приложение раз", "proposed", "2026-01-01T10:00")])
    sj.assign(db, settings, project.id, embed=fake_embed, ask=lambda s, u: "{}")
    from app.app.models.artifact import Artifact

    art2 = Artifact(artifact_id="art_subj2", project_id=project.id, user_id=1, original_filename="второй.html", stored_filename="c2", file_path="/x",
                    file_type="html", source_type="document", sha256="subj2", status="done")
    db.add(art2)
    db.commit()
    _acts(db, project, art2, [("приложение снова", "приложение два", "agreed", "2026-02-01T10:00"), ("цена", "цена", "info", "2026-02-02T10:00")])
    stats = sj.assign(db, settings, project.id, embed=fake_embed, ask=lambda s, u: "{}")
    assert stats["acts"] == 2 and stats["attached"] == 1 and stats["created"] == 1 and stats["subjects"] == 2  # прежний предмет дополнен, не пересоздан
    ids = {a.unit_ref + str(a.artifact_id): a.subject_id for a in db.query(SpeechAct).all()}
    assert len({v for v in ids.values()}) == 2
    again = sj.assign(db, settings, project.id, embed=fake_embed, ask=lambda s, u: "{}")
    assert again["acts"] == 0  # всё уже распределено


def test_rebuild_keeps_subjects_with_human_events_and_build_events(world):
    from app.app.models.decision_memory import DecisionEvent, Subject
    from app.app.models.speech_act import SpeechAct
    from app.app.services import decision_log as dl

    db, settings, project, art = world
    kept = Subject(project_id=project.id, title="Решено владельцем")
    db.add(kept)
    db.commit()
    dl.add_event(db, kept, "agreed", "2026-01-09", precision="day", decided_by="human")
    _acts(db, project, art, [("приложение", "приложение раз", "proposed", "2026-01-01T10:00"), ("приложение", "приложение два", "agreed", "2026-01-02T10:00")])
    first = sj.assign(db, settings, project.id, embed=fake_embed, ask=lambda s, u: "{}")
    assert first["subjects"] == 2  # владельческий предмет + новый
    made = sj.build_events(db, project.id)
    assert made["events"] == 2 and db.query(DecisionEvent).filter(DecisionEvent.decided_by == "rule").count() == 2
    again = sj.assign(db, settings, project.id, rebuild=True, embed=fake_embed, ask=lambda s, u: "{}")
    assert again["created"] == 1 and db.get(Subject, kept.id) is not None  # предмет с решением человека пережил пересборку
    sj.build_events(db, project.id)
    new_subject = db.query(Subject).filter(Subject.id != kept.id).one()
    assert new_subject.state == "proposed" or new_subject.state == "agreed"
    assert db.query(SpeechAct).filter(SpeechAct.subject_id.is_(None)).count() == 0
