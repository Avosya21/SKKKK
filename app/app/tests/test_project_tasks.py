from __future__ import annotations

import pytest
from sqlalchemy import select

from app.app.models.project import Project
from app.app.models.project_task import ProjectTask
from app.app.models.user import User
from app.app.services.project_tasks import (
    TASK_TZ_REBUILD,
    TaskAlreadyRunning,
    begin_task,
    get_running_task,
    list_active_tasks,
    track_project_task,
)


def _create_project(db) -> Project:
    user = db.scalar(select(User).limit(1))
    p = Project(name="TaskProj", description="", created_by=user.id, is_active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_begin_task_blocks_duplicate(db_engine, client, admin_token):
    from app.app.db import SessionLocal

    db = SessionLocal()
    p = _create_project(db)
    begin_task(db, task_type=TASK_TZ_REBUILD, project_id=p.id)
    with pytest.raises(TaskAlreadyRunning):
        begin_task(db, task_type=TASK_TZ_REBUILD, project_id=p.id)
    db.close()


def test_active_tasks_endpoint(client, admin_token, manager_token):
    from app.app.db import SessionLocal

    headers = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "ActiveTask"}, headers=headers).json()["id"]

    db = SessionLocal()
    begin_task(db, task_type=TASK_TZ_REBUILD, project_id=pid)
    db.close()

    r = client.get(
        f"/projects/{pid}/tasks/active",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["task_type"] == TASK_TZ_REBUILD
    assert tasks[0]["status"] == "running"


def test_rebuild_tz_returns_409_when_running(client, admin_token, manager_token):
    from app.app.db import SessionLocal

    headers = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "Conflict"}, headers=headers).json()["id"]

    db = SessionLocal()
    begin_task(db, task_type=TASK_TZ_REBUILD, project_id=pid)
    db.close()

    r = client.post(
        f"/projects/{pid}/rebuild-tz",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 409


def test_track_project_task_marks_done(db_engine, client, admin_token):
    from app.app.db import SessionLocal

    db = SessionLocal()
    p = _create_project(db)
    with track_project_task(db, task_type=TASK_TZ_REBUILD, project_id=p.id):
        assert get_running_task(db, task_type=TASK_TZ_REBUILD, project_id=p.id) is not None
    row = db.scalar(select(ProjectTask).where(ProjectTask.project_id == p.id))
    assert row.status == "done"
    assert list_active_tasks(db, project_id=p.id) == []
    db.close()
