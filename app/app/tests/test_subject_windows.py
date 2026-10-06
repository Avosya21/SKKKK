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
                                "titles": {"N1": "Разбивка каталога по толщине", "N2": "Оплата подписки"}}),
                    "не json"])  # на вопрос про оставшийся акт модель не ответила
    result = sw.assign(db, settings, project.id, embed=lambda texts: [[1.0, 0.0]] * len(texts), ask=lambda s, u: next(answers))
    assert result["windows"] == 1 and result["created"] == 3 and result["unmarked"] == 1 and result["errors"] == 1
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


def test_parse_same():
    assert sw.parse_same('{"same": true}') is True and sw.parse_same('```json\n{"same": false}\n```') is False
    with pytest.raises(ValueError):
        sw.parse_same('{"same": "да"}')


def test_merge_similar_moves_acts_and_deletes_smaller(world):
    from app.app.models.decision_memory import Subject
    from app.app.models.speech_act import SpeechAct

    db, settings, project, acts = world
    big = Subject(project_id=project.id, title="Цена работ", embedding=sw.pack([1.0, 0.0]))
    small = Subject(project_id=project.id, title="Стоимость", embedding=sw.pack([0.99, 0.1]))
    other = Subject(project_id=project.id, title="Оплата", embedding=sw.pack([0.0, 1.0]))
    db.add_all([big, small, other])
    db.commit()
    for a, s in zip(acts, [big, big, small, other, other]):
        a.subject_id = s.id
    db.commit()
    questions: list[str] = []

    def ask(system, user):
        questions.append(user)
        return '{"same": true}'

    result = sw.merge_similar(db, settings, project.id, ask=ask)
    assert result == {"pairs": 1, "merged": 1, "errors": 0} and len(questions) == 1  # «Оплата» далеко — не спрашиваем
    assert db.get(Subject, small.id) is None
    assert {a.subject_id for a in db.query(SpeechAct).filter(SpeechAct.id.in_([x.id for x in acts[:3]])).all()} == {big.id}


def test_merge_keeps_subjects_when_model_says_different_or_fails(world):
    from app.app.models.decision_memory import Subject

    db, settings, project, acts = world
    a = Subject(project_id=project.id, title="Цена", embedding=sw.pack([1.0, 0.0]))
    b = Subject(project_id=project.id, title="Цена 2", embedding=sw.pack([1.0, 0.05]))
    db.add_all([a, b])
    db.commit()
    assert sw.merge_similar(db, settings, project.id, ask=lambda s, u: '{"same": false}')["merged"] == 0
    assert sw.merge_similar(db, settings, project.id, ask=lambda s, u: "не json")["errors"] == 1
    assert db.query(Subject).filter(Subject.project_id == project.id).count() == 2


def test_unmarked_acts_get_one_followup_question(world):
    from app.app.models.decision_memory import Subject

    db, settings, project, acts = world
    ids = [a.id for a in acts]
    prompts: list[str] = []

    def ask(system, user):
        prompts.append(user)
        if len(prompts) == 1:  # модель разметила только первые три акта
            return json.dumps({"items": [{"act": f"A{i}", "subject": "N1"} for i in ids[:3]], "titles": {"N1": "Разбивка каталога"}}, ensure_ascii=False)
        return json.dumps({"items": [{"act": f"A{i}", "subject": "N2"} for i in ids[3:]], "titles": {"N2": "Оплата"}}, ensure_ascii=False)

    result = sw.assign(db, settings, project.id, embed=lambda texts: [[1.0, 0.0]] * len(texts), ask=ask)
    assert len(prompts) == 2 and result["followups"] == 1 and result["unmarked"] == 0 and result["created"] == 2
    assert "Разметь ТОЛЬКО акты ниже; новые метки начинай с N2." in prompts[1] and "N1 Разбивка каталога" in prompts[1]
    assert f"A{ids[0]}" not in prompts[1].split("Акты окна:")[1]  # во втором вопросе только оставшиеся акты
    assert {s.title for s in db.query(Subject).filter(Subject.project_id == project.id).all()} == {"Разбивка каталога", "Оплата"}
