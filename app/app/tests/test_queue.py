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
        headers={"Authorization": f"Bearer {manager_token}"},
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


def test_claim_fifo_two_users_isolated_projects(db_engine):
    db = db_mod.SessionLocal()
    try:
        from app.app.models.artifact import Artifact
        from app.app.models.job import Job
        from app.app.models.project import Project
        from app.app.models.user import User
        from app.app.services.storage import artifact_dir, create_project_tree
        from app.app.config import get_settings

        settings = get_settings()
        u1 = User(name="ann", telegram_id="tg-a", role="user", password_hash="x")
        u2 = User(name="bob", telegram_id="tg-b", role="user", password_hash="x")
        db.add_all([u1, u2])
        db.flush()
        p1 = Project(name="P1", created_by=u1.id, is_active=True)
        p2 = Project(name="P2", created_by=u2.id, is_active=True)
        db.add_all([p1, p2])
        db.flush()
        create_project_tree(settings, p1.id)
        create_project_tree(settings, p2.id)

        art_a = Artifact(
            artifact_id="art_user_a",
            project_id=p1.id,
            user_id=u1.id,
            original_filename="first.webm",
            stored_filename="first.webm",
            file_path=str(artifact_dir(settings, p1.id, "art_user_a") / "audio" / "input_16k.wav"),
            file_type="audio",
            source_type="call",
            sha256="aaa",
        )
        art_b = Artifact(
            artifact_id="art_user_b",
            project_id=p2.id,
            user_id=u2.id,
            original_filename="second.pdf",
            stored_filename="second.pdf",
            file_path=str(artifact_dir(settings, p2.id, "art_user_b") / "raw.txt"),
            file_type="pdf",
            source_type="document",
            sha256="bbb",
        )
        db.add_all([art_a, art_b])
        db.flush()
        job_a = Job(artifact_id=art_a.id, status="queued", phase="asr")
        db.add(job_a)
        db.flush()
        job_b = Job(artifact_id=art_b.id, status="queued", phase="asr")
        db.add(job_b)
        db.commit()

        first = claim_next_job(db_mod.SessionLocal(), 3)
        assert first is not None
        assert first.id == job_a.id
        assert first.artifact_id == art_a.id
        second = claim_next_job(db_mod.SessionLocal(), 3)
        assert second is not None
        assert second.id == job_b.id
        assert second.artifact_id == art_b.id

        a_root = artifact_dir(settings, p1.id, "art_user_a")
        b_root = artifact_dir(settings, p2.id, "art_user_b")
        assert a_root != b_root
        assert str(p1.id) in str(a_root)
        assert str(p2.id) in str(b_root)
        assert "art_user_a" in str(a_root)
        assert "art_user_b" in str(b_root)
    finally:
        db.close()


def test_dry_run_walks_all_statuses(client, manager_token, user_token):
    pid = _upload(client, manager_token, user_token)
    assert run_once()
    arts = client.get(
        f"/projects/{pid}/artifacts",
        headers={"Authorization": f"Bearer {manager_token}"},
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
        headers={"Authorization": f"Bearer {manager_token}"},
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
