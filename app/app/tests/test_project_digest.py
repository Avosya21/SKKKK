"""«Что нового с прошлого захода» (PORTAL-ACCURACY-MANAGER-PLAN, Ш5): отсчёт заходов, строки, «Скрыть», роли."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

from app.app.services import project_digest as pd

T0 = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)


def _setup(client, manager_token):
    from app.app.db import SessionLocal

    h = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "Что нового"}, headers=h).json()["id"]
    return SessionLocal(), pid


def _material(db, pid, finished: datetime, *, tasks=(), decisions=0):
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.models.job import Job

    tag = uuid.uuid4().hex[:8]
    art = Artifact(artifact_id=f"art_2026-10-13_090000_{tag}", project_id=pid, user_id=1, original_filename=f"{tag}.docx",
                   stored_filename=f"{tag}.docx", file_path="x", file_type="docx", source_type="document", sha256=tag.ljust(64, "0"),
                   status="done")
    db.add(art)
    db.flush()
    db.add(Job(artifact_id=art.id, kind="process_artifact", status="done", finished_at=finished))
    for value in tasks:
        db.add(Entity(artifact_id=art.id, entity_type="task", value_json=json.dumps(value, ensure_ascii=False)))
    for i in range(decisions):
        db.add(Entity(artifact_id=art.id, entity_type="decision", value_json=json.dumps({"text": f"Решение {i}"}, ensure_ascii=False)))
    db.add(Entity(artifact_id=art.id, entity_type="decision",
                  value_json=json.dumps({"text": "Старое", "relevance": {"state": "duplicate"}}, ensure_ascii=False)))
    db.commit()
    return art


def test_visit_rotation(client, manager_token):
    db, pid = _setup(client, manager_token)
    v = pd.touch_visit(db, 1, pid, now=T0)
    assert v.prev_seen_at is None
    v = pd.touch_visit(db, 1, pid, now=T0 + timedelta(minutes=20))  # тот же заход — отсчёта ещё нет
    assert v.prev_seen_at is None
    v = pd.touch_visit(db, 1, pid, now=T0 + timedelta(hours=3))  # новый заход: прошлый стал отсчётом
    assert pd._aware(v.prev_seen_at) == T0 + timedelta(minutes=20)
    v = pd.touch_visit(db, 1, pid, now=T0 + timedelta(hours=3, minutes=10))  # перезагрузка не сбрасывает «новое»
    assert pd._aware(v.prev_seen_at) == T0 + timedelta(minutes=20)
    db.close()


def test_lines_dismiss_and_roles(client, manager_token):
    from app.app.config import get_settings
    from app.app.models.project import Project

    db, pid = _setup(client, manager_token)
    settings = get_settings()
    project = db.get(Project, pid)
    pd.touch_visit(db, 1, pid, now=T0)
    first = pd.build_digest(db, settings, project, pd.touch_visit(db, 1, pid, now=T0), role="manager")
    assert first["first_visit"] and first["lines"] == []
    _material(db, pid, T0 - timedelta(hours=1))  # обработан до отсчёта — не новый
    _material(db, pid, T0 + timedelta(hours=1), decisions=2,
              tasks=({"description": "Прислать остатки", "deadline": "01.10.2026", "status": "open"},))
    visit = pd.touch_visit(db, 1, pid, now=T0 + timedelta(hours=4))
    out = pd.build_digest(db, settings, project, visit, role="manager", today=date(2026, 10, 14))
    keys = [line["key"] for line in out["lines"]]
    assert keys == ["materials", "tasks"]
    assert out["lines"][0]["text"] == "Обработано новых материалов: 1 — из них 2 решения и требования, 1 задача"
    assert out["lines"][1]["view"] == "overdue" and "просрочено 1" in out["lines"][1]["text"]
    assert out["since_label"]

    pd.dismiss(db, 1, pid, now=T0 + timedelta(hours=4, minutes=1))
    visit = pd.touch_visit(db, 1, pid, now=T0 + timedelta(hours=4, minutes=2))
    assert pd.build_digest(db, settings, project, visit, role="manager", today=date(2026, 10, 14))["lines"] == []
    _material(db, pid, T0 + timedelta(hours=4, minutes=5))  # новый материал после «Скрыть» — полоска снова видна
    assert pd.build_digest(db, settings, project, visit, role="manager", today=date(2026, 10, 14))["lines"]
    user_view = pd.build_digest(db, settings, project, visit, role="user", today=date(2026, 10, 14))
    assert {line["key"] for line in user_view["lines"]} <= {"materials", "tasks"}
    db.close()


def test_routes(client, manager_token):
    h = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "Что нового: роуты"}, headers=h).json()["id"]
    r = client.get(f"/projects/{pid}/digest", headers=h)
    assert r.status_code == 200 and r.json()["first_visit"] is True and r.json()["lines"] == []
    assert client.post(f"/projects/{pid}/digest/dismiss", headers=h).status_code == 200
    assert client.get("/projects/999999/digest", headers=h).status_code == 404


def test_plural():
    assert pd.plural(1, ("задача", "задачи", "задач")) == "1 задача"
    assert pd.plural(3, ("задача", "задачи", "задач")) == "3 задачи"
    assert pd.plural(11, ("задача", "задачи", "задач")) == "11 задач"
    assert pd.plural(22, ("задача", "задачи", "задач")) == "22 задачи"
