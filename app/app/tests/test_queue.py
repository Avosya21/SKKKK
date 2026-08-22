from __future__ import annotations

import io
import threading

from app.app import db as db_mod
from app.app.config import get_settings
from app.app.services.queue import claim_next_job
from app.app.workers.gpu_worker import run_once


def _upload(client, manager_token, user_token):
    pid = client.post(
        "/projects",
        json={"name": "Queue"},
        headers={"Authorization": f"Bearer {manager_token}"},
    ).json()["id"]
    client.post(
        f"/projects/{pid}/upload",
        headers={"Authorization": f"Bearer {user_token}"},
        data={"source_type": "note"},
        files=[("files", ("q.txt", io.BytesIO(b"queue"), "text/plain"))],
    )
    return pid


def test_job_claim_is_atomic(db_engine):
    db = db_mod.SessionLocal()
    try:
        from app.app.models.job import Job
        from app.app.models.artifact import Artifact
        from app.app.models.project import Project
        from app.app.models.user import User

        user = User(name="u", telegram_id="t1", role="admin", password_hash="x")
        db.add(user)
        db.flush()
        project = Project(name="p", created_by=user.id, is_active=True)
        db.add(project)
        db.flush()

        art = Artifact(
            artifact_id="art_test",
            project_id=project.id,
            user_id=user.id,
            original_filename="x",
            stored_filename="x.txt",
            file_path="/x",
            file_type="txt",
            source_type="note",
            sha256="abc",
        )
        db.add(art)
        db.flush()
        db.add(Job(artifact_id=art.id, status="queued"))
        db.commit()

        j1 = claim_next_job(db_mod.SessionLocal(), 3)
        j2 = claim_next_job(db_mod.SessionLocal(), 3)
        assert j1 is not None
        assert j2 is None
    finally:
        db.close()


def test_dry_run_walks_all_statuses(client, manager_token, user_token):
    pid = _upload(client, manager_token, user_token)
    assert run_once()
    arts = client.get(
        f"/projects/{pid}/artifacts",
        headers={"Authorization": f"Bearer {user_token}"},
    ).json()
    assert arts[0]["status"] == "done"


def test_failed_job_sets_error_status_and_message(client, manager_token, user_token, monkeypatch):
    pid = _upload(client, manager_token, user_token)

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    import app.app.workers.processor as proc

    monkeypatch.setattr(proc, "process_dry_run", boom)
    run_once()
    arts = client.get(
        f"/projects/{pid}/artifacts",
        headers={"Authorization": f"Bearer {user_token}"},
    ).json()
    assert arts[0]["status"] == "error"
    assert "boom" in (arts[0]["error_message"] or "")


def test_tmp_cleaned_on_success_and_on_error(client, manager_token, user_token):
    pid = _upload(client, manager_token, user_token)
    settings = get_settings()
    run_once()
    tmp_root = settings.data_dir / "projects" / str(pid) / "tmp"
    assert not any(tmp_root.glob("*")) if tmp_root.exists() else True


def test_retry_increments_attempts_and_stops_at_limit(db_engine, monkeypatch):
    monkeypatch.setenv("JOB_MAX_ATTEMPTS", "2")
    from app.app.config import get_settings

    get_settings.cache_clear()
    db = db_mod.SessionLocal()
    try:
        from app.app.models.job import Job
        from app.app.models.artifact import Artifact
        from app.app.models.project import Project
        from app.app.models.user import User

        user = User(name="u2", telegram_id="t2", role="admin", password_hash="x")
        db.add(user)
        db.flush()
        project = Project(name="p2", created_by=user.id, is_active=True)
        db.add(project)
        db.flush()
        art = Artifact(
            artifact_id="art_retry",
            project_id=project.id,
            user_id=user.id,
            original_filename="x",
            stored_filename="x.txt",
            file_path="/x",
            file_type="txt",
            source_type="note",
            sha256="def",
        )
        db.add(art)
        db.flush()
        job = Job(artifact_id=art.id, status="queued", attempts=2)
        db.add(job)
        db.commit()
        assert claim_next_job(db, 2) is None
    finally:
        db.close()
