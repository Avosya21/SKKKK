"""Память решений в очереди и инкремент (docs/plans/DECISION-MEMORY-STAGES-6-8.md, Э8). Модель и векторы подменены; имена вымышленные."""
from __future__ import annotations

import json

import pytest

from app.app.services import memory_mode
from app.app.services import memory_pipeline as mp


@pytest.fixture()
def world(db_engine, tmp_data, monkeypatch):
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services import chat_export

    monkeypatch.setattr(get_settings(), "decision_memory_mode", "off")  # тесты не зависят от локального .env

    db = SessionLocal()
    user = User(name="pipe-user", telegram_id="tg-pipe", role="admin", password_hash="x")
    db.add(user)
    db.commit()
    project = Project(name="Очередь памяти", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()
    arts = []
    for i in (1, 2):
        art = Artifact(artifact_id=f"art_pipe{i}", project_id=project.id, user_id=user.id, original_filename=f"чат{i}.html", stored_filename="c", file_path="/x",
                       file_type="html", source_type="document", sha256=f"pipe{i}", status="done")
        db.add(art)
        arts.append(art)
    db.commit()
    exports = {
        arts[0].id: {"messages": [{"id": 1, "at": "2026-02-01T10:00:00", "author": "Ольга", "text": "Давайте сделаем отдельное приложение для менеджеров."},
                                  {"id": 2, "at": "2026-02-01T10:05:00", "author": "Павел", "text": "Согласен, стартуем в понедельник."}]},
        arts[1].id: {"messages": [{"id": 1, "at": "2026-02-08T10:00:00", "author": "Ольга", "text": "Приложение для менеджеров отменяем, делаем веб-версию."}]},
    }
    monkeypatch.setattr(chat_export, "load_export", lambda path: next(exports[a.id] for a in arts if a.artifact_id in str(path)))
    yield db, get_settings(), project, arts, exports
    db.close()


def test_off_project_does_nothing_and_queue_respects_mode(world):
    from app.app.models.job import Job

    db, settings, project, arts, _ = world
    assert mp.enqueue_for_artifact(db, settings, arts[0], "shadow") == []  # режим off
    assert mp.memory_for_review(db, settings, arts[0], "shadow") == {"skipped": "память решений выключена"}
    assert mp.memory_judge_for_review(db, settings, arts[0], "shadow") == {"skipped": "память решений выключена"}
    memory_mode.set_mode(db, project, "shadow")
    assert mp.enqueue_for_artifact(db, settings, arts[0], "shadow") == ["memory", "memory_judge"]
    assert mp.enqueue_for_artifact(db, settings, arts[0], "shadow") == []  # дубль не ставится
    assert db.query(Job).filter(Job.artifact_id == arts[0].id).count() == 2


def test_queue_project_sets_shadow_and_queues_all_materials(world):
    from app.app.models.job import Job

    db, settings, project, arts, _ = world
    result = mp.queue_project(db, settings, project.id)
    assert result["materials"] == 2 and result["jobs_queued"] == 3  # акты каждого материала + один итог
    assert memory_mode.mode_of(settings, project) == "shadow"
    scopes = sorted(json.loads(j.payload_json)["scope"] for j in db.query(Job).all())
    assert scopes == ["memory", "memory", "memory_judge"]


def test_judge_task_is_deferred_while_acts_or_later_judge_wait(world):
    db, settings, project, arts, _ = world
    memory_mode.set_mode(db, project, "shadow")
    mp.enqueue_for_artifact(db, settings, arts[0], "shadow")
    mp.enqueue_for_artifact(db, settings, arts[1], "shadow")
    # итог первого материала ждёт: акты второго ещё в очереди
    assert "deferred" in mp.memory_judge_for_review(db, settings, arts[0], "shadow")


def test_incremental_build_keeps_unchanged_judgements(world, monkeypatch):
    from app.app.models.decision_memory import Subject
    from app.app.models.speech_act import SpeechAct
    from app.app.services import speech_acts, subject_judge

    db, settings, project, arts, exports = world
    memory_mode.set_mode(db, project, "shadow")
    calls = {"judge": 0, "window": 0}

    def acts_ask(system, user):
        if "Черновая разметка:" in user:
            return user.split("Черновая разметка:\n", 1)[1]  # проверяющий оставляет черновик
        if "Давайте сделаем" in user:
            return json.dumps({"acts": [{"unit": "M1", "act": "proposed", "quote": "сделаем отдельное приложение", "about": "приложение"},
                                        {"unit": "M2", "act": "agreed", "quote": "Согласен", "about": "приложение"}]}, ensure_ascii=False)
        return json.dumps({"acts": [{"unit": "M1", "act": "cancelled", "quote": "отменяем", "about": "приложение"}]}, ensure_ascii=False)

    def window_ask(system, user):
        calls["window"] += 1
        ids = [line.split(" ", 1)[0] for line in user.splitlines() if line.startswith("A")]
        known = [line.split(" ", 1)[0] for line in user.splitlines() if line.startswith("S")]
        return json.dumps({"items": [{"act": i, "subject": known[0] if known else "N1"} for i in ids], "titles": {"N1": "Приложение для менеджеров"}}, ensure_ascii=False)

    def judge_ask(system, user):
        calls["judge"] += 1
        acts = [int(line.split(" ", 1)[0][1:]) for line in user.splitlines() if line.startswith("A")]
        return json.dumps({"state": "agreed", "basis": [f"A{acts[-1]}"], "summary": "итог",
                           "positions": [{"label": "приложение", "state": "current", "basis": [f"A{acts[-1]}"]}]}, ensure_ascii=False)

    def ask(system, user):
        if "Известные предметы:" in user:
            return window_ask(system, user)
        if "Лента:" in user:
            return judge_ask(system, user)
        if "Метод A:" in user:
            return json.dumps({"truth": "active", "reason": "ok"})
        if "Предмет «" in user:
            return json.dumps({"same": True})
        return acts_ask(system, user)

    speech_acts.extract_artifact(db, settings, arts[0], ask=ask)
    first = mp.build_project(db, settings, project.id, ask=ask, embed=lambda texts: [[1.0, 0.0]] * len(texts))
    assert first["subjects"]["created"] == 1 and first["judge"]["applied"] == 1 and calls["judge"] >= 1
    subject = db.query(Subject).filter(Subject.project_id == project.id).one()
    assert subject.state_source == "judge" and subject.judged_sig == subject_judge.act_signature(db, subject.id)
    judged_calls = calls["judge"]
    again = mp.build_project(db, settings, project.id, ask=ask, embed=lambda texts: [[1.0, 0.0]] * len(texts))  # ничего нового
    assert again["subjects"]["acts"] == 0 and again["restored"] == 1 and again["judge"]["subjects"] == 0 and calls["judge"] == judged_calls
    db.expire_all()
    assert db.query(Subject).filter(Subject.project_id == project.id).one().state_source == "judge"
    # второй материал: акт попадает в известный предмет, итог пересчитывается только у него
    speech_acts.extract_artifact(db, settings, arts[1], ask=ask)
    third = mp.build_project(db, settings, project.id, ask=ask, embed=lambda texts: [[1.0, 0.0]] * len(texts))
    assert third["subjects"]["attached"] == 1 and third["restored"] == 0 and third["judge"]["applied"] == 1 and calls["judge"] == judged_calls + 1
    assert db.query(SpeechAct).filter(SpeechAct.project_id == project.id, SpeechAct.subject_id.is_not(None)).count() == 3


def test_empty_subjects_are_retired(world):
    from app.app.models.decision_memory import Subject

    db, settings, project, arts, _ = world
    db.add(Subject(project_id=project.id, title="Без актов"))
    db.commit()
    assert mp.retire_empty_subjects(db, project.id) == 1
    assert db.query(Subject).filter(Subject.project_id == project.id, Subject.is_active.is_(True)).count() == 0


def test_api_build_is_admin_only(client, admin_token, manager_token, world):
    pid = world[2].id
    manager = {"Authorization": f"Bearer {manager_token}"}
    admin = {"Authorization": f"Bearer {admin_token}"}
    assert client.post(f"/projects/{pid}/memory/build", headers=manager).status_code in (403, 404)
    r = client.post(f"/projects/{pid}/memory/build", headers=admin)
    assert r.status_code == 200 and r.json()["jobs_queued"] == 3


def test_build_is_refused_while_previous_build_runs(client, admin_token, world):
    from app.app.models.job import Job

    db, settings, project, arts, _ = world
    admin = {"Authorization": f"Bearer {admin_token}"}
    assert client.post(f"/projects/{project.id}/memory/build", headers=admin).status_code == 200
    assert client.get(f"/projects/{project.id}/memory/summary", headers=admin).json()["building"] == 3
    r = client.post(f"/projects/{project.id}/memory/build", headers=admin)
    assert r.status_code == 409 and "уже строится" in r.json()["detail"]
    with pytest.raises(ValueError):
        mp.queue_project(db, settings, project.id)
    db.query(Job).update({Job.status: "done"})
    db.commit()
    assert client.get(f"/projects/{project.id}/memory/summary", headers=admin).json()["building"] == 0
    assert client.post(f"/projects/{project.id}/memory/build", headers=admin).status_code == 200  # после окончания — снова можно
