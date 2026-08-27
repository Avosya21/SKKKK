from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.app.models.job import Job


def requeue_orphaned_jobs(db: Session, max_attempts: int) -> int:
    q = (
        update(Job)
        .where(Job.status == "running")
        .values(status="queued", started_at=None, error_message="requeued after worker restart")
    )
    result = db.execute(q)
    db.commit()
    return result.rowcount or 0


def claim_next_job(db: Session, max_attempts: int) -> Job | None:
    job = db.scalar(
        select(Job)
        .where(Job.status == "queued", Job.attempts < max_attempts)
        .order_by(Job.created_at, Job.id)
        .limit(1)
    )
    if not job:
        return None
    res = db.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == "queued")
        .values(
            status="running",
            attempts=job.attempts + 1,
            started_at=datetime.now(timezone.utc),
            error_message=None,
        )
    )
    if res.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    return db.get(Job, job.id)


def finish_job(db: Session, job: Job, *, ok: bool, error: str | None = None) -> None:
    job.status = "done" if ok else "error"
    job.finished_at = datetime.now(timezone.utc)
    job.error_message = error
    db.commit()


def enqueue_process_artifact(db: Session, artifact_id: int, *, phase: str = "asr") -> Job:
    job = Job(
        artifact_id=artifact_id,
        kind="process_artifact",
        status="queued",
        phase=phase,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def enqueue_knowledge_retry(db: Session, artifact_id: int) -> Job:
    """Queue knowledge-only retry when ASR/transcript artifacts already exist."""
    return enqueue_process_artifact(db, artifact_id, phase="knowledge")
