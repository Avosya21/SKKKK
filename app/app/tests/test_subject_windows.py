"""Предметы одним проходом по окну (docs/plans/DECISION-MEMORY-PLAN.md, §13). Модель и векторы подменены; имена вымышленные."""
from __future__ import annotations

import json

import pytest

from app.app.services import subject_windows as sw


def test_parse_window_filters_unknown_acts_and_labels():
    marks, titles = sw.parse_window(json.dumps({"items": [{"act": "A1", "subject": "S5"}, {"act": "A2", "subject": "n1"}, {"act": "A9", "subject": "S5"},
                                                         {"act": "A3", "subject": "S77"}, "мусор"], "titles": {"n1": " Новый вопрос ", "N2": 5}}), {1, 2, 3}, {5})
    assert marks == {1: "S5", 2: "N1"} and titles == {"N1": "Новый вопрос"}  # S77 не показан, A9 не из окна
    with pytest.raises(ValueError):
        sw.parse_window('{"items": []}', {1}, set())
    with pytest.raises(ValueError):
        sw.parse_window('{"нет": 1}', {1}, set())
    with pytest.raises(json.JSONDecodeError):
        sw.parse_window("не json", {1}, set())


@pytest.fixture()
def world(db_engine, tmp_data):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.speech_act import SpeechAct
    from app.app.models.user import User

    db = SessionLocal()
    user = User(name="win-user", telegram_id="tg-win", role="admin", password_hash="x")
    db.add(user)
    db.commit()
    project = Project(name="Окна", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()
    art = Artifact(artifact_id="art_win", project_id=project.id, user_id=user.id, original_filename="чат.html", stored_filename="c", file_path="/x",
                   file_type="html", source_type="document", sha256="win", status="done")
    db.add(art)
    db.commit()
    spec = [("proposed", "А мы можем по толщине разбить?"), ("rejected", "Папки по толщине — неправильно"), ("agreed", "Давайте как вы показали"),
            ("proposed", "Нужна оплата подписки"), ("done", "Подписку оплатили")]
    acts = []
    for i, (act, quote) in enumerate(spec, 1):
        row = SpeechAct(project_id=project.id, artifact_id=art.id, unit_ref=f"M{i}", unit_key=i, author="Автор", act=act, quote=quote,
                        said_at=f"2026-09-11T12:0{i}:00", prompt_version="v", model="m")
        db.add(row)
        acts.append(row)
    db.commit()
    yield db, get_settings(), project, acts
    db.close()


def test_assign_groups_acts_by_model_labels(world):
    from sqlalchemy import select

    from app.app.models.decision_memory import Subject

    db, settings, project, acts = world
    ids = [a.id for a in acts]
    answers = iter([json.dumps({"items": [{"act": f"A{ids[0]}", "subject": "N1"}, {"act": f"A{ids[1]}", "subject": "N1"}, {"act": f"A{ids[2]}", "subject": "N1"},
                                          {"act": f"A{ids[3]}", "subject": "N2"}],  # пятый акт не размечен
                                "titles": {"N1": "Разбивка каталога по толщине", "N2": "Оплата подписки"}})])
    result = sw.assign(db, settings, project.id, embed=lambda texts: [[1.0, 0.0]] * len(texts), ask=lambda s, u: next(answers))
    assert result["windows"] == 1 and result["created"] == 3 and result["unmarked"] == 1 and result["errors"] == 0
    titles = {s.id: s.title for s in db.scalars(select(Subject).where(Subject.project_id == project.id)).all()}
    first, fourth = (db.get(type(acts[0]), i) for i in (ids[0], ids[3]))
    assert titles[first.subject_id] == "Разбивка каталога по толщине" and titles[fourth.subject_id] == "Оплата подписки"
    assert {db.get(type(acts[0]), i).subject_id for i in ids[:3]} == {first.subject_id}
    assert db.get(type(acts[0]), ids[4]).subject_id not in (first.subject_id, fourth.subject_id)


def test_second_window_attaches_to_known_subject(world, monkeypatch):
    db, settings, project, acts = world
    monkeypatch.setattr(sw, "WINDOW_ACTS", 3)
    ids = [a.id for a in acts]
    seen: list[str] = []

    def ask(system, user):
        seen.append(user)
        if len(seen) == 1:
            return json.dumps({"items": [{"act": f"A{i}", "subject": "N1"} for i in ids[:3]], "titles": {"N1": "Разбивка по толщине"}})
        subject_id = db.get(type(acts[0]), ids[0]).subject_id
        return json.dumps({"items": [{"act": f"A{ids[3]}", "subject": "N1"}, {"act": f"A{ids[4]}", "subject": f"S{subject_id}"}], "titles": {"N1": "Оплата"}})

    result = sw.assign(db, settings, project.id, embed=lambda texts: [[1.0, 0.0]] * len(texts), ask=ask)
    assert result["windows"] == 2 and result["attached"] == 1 and result["created"] == 2
    assert "Разбивка по толщине" in seen[1] and "Акты окна:" in seen[1]  # известный предмет показан во втором окне
    assert db.get(type(acts[0]), ids[4]).subject_id == db.get(type(acts[0]), ids[0]).subject_id


def test_invalid_answer_retried_then_acts_become_own_subjects(world):
    db, settings, project, acts = world
    calls: list[int] = []

    def ask(system, user):
        calls.append(1)
        return "не json"

    result = sw.assign(db, settings, project.id, embed=lambda texts: [[1.0, 0.0]] * len(texts), ask=ask)
    assert len(calls) == 2 and result["errors"] == 1 and result["unmarked"] == 5 and result["created"] == 5
