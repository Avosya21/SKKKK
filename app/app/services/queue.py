"""Очередь задач (таблица jobs): захват следующей задачи, завершение, возврат «осиротевших», постановка обработки и повтора этапа знаний."""

from __future__ import annotations

from datetime import datetime, timezone

import json
import re
from pathlib import Path

from sqlalchemy import case, select, text, update
from sqlalchemy.orm import Session

from app.app.models.job import Job

KIND_PROCESS = "process_artifact"
KIND_AUTO_REVIEW = "auto_review"
KIND_REFRESH = "refresh_knowledge"  # после имени голоса у готового созвона: переиндексация чанков и выжимка
REVIEW_SCOPES = ("artifact", "conflicts")


def requeue_orphaned_jobs(db: Session, max_attempts: int) -> int:
    exhausted = db.execute(
        update(Job)
        .where(Job.status == "running", Job.attempts >= max_attempts)
        .values(status="error", error_message="max attempts after worker restart")
    )
    db.commit()
    q = (
        update(Job)
        .where(Job.status == "running", Job.attempts < max_attempts)
        .values(status="queued", started_at=None, error_message="requeued after worker restart")
    )
    result = db.execute(q)
    db.commit()
    return (result.rowcount or 0) + (exhausted.rowcount or 0)


def claim_next_job(db: Session, max_attempts: int) -> Job | None:
    db.commit()
    try:
        db.execute(text("BEGIN IMMEDIATE"))
    except Exception as exc:
        # SQLAlchemy already opened a transaction after commit(); SQLite then
        # rejects a nested BEGIN. The UPDATE ... WHERE status='queued' below
        # still serializes claims for the single GPU worker.
        if "within a transaction" not in str(exc).lower() and "cannot start" not in str(exc).lower():
            raise
    # Расшифровка и знания всегда раньше фоновой автопроверки: она не должна задерживать новые файлы.
    priority = case((Job.kind == KIND_PROCESS, 0), else_=1)
    job = db.scalar(
        select(Job)
        .where(Job.status == "queued", Job.attempts < max_attempts)
        .order_by(priority, Job.created_at, Job.id)
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
        kind=KIND_PROCESS,
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


def running_worker_kinds(data_dir: Path) -> set[str]:
    """Виды задач, которые понимает запущенный воркер: из последней строки ``worker.started`` в журнале.

    Воркер старой версии не пишет ``kinds=`` и любую задачу выполняет как расшифровку — на готовом созвоне это полная
    переобработка. Поэтому новые виды задач ставим только когда запущенный воркер их понимает (урок 2026-09-23).
    Воркер не запущен (нет pid-файла) — задачи дождутся его, и новый воркер их поймёт: считаем, что понимает всё.
    """
    pid_file = data_dir / "locks" / "gpu_worker.pid"
    log = data_dir / "logs" / "worker.log"
    if not pid_file.is_file() or not log.is_file():
        return {KIND_PROCESS, KIND_AUTO_REVIEW, KIND_REFRESH}
    pid = pid_file.read_text(encoding="utf-8").strip()
    last = ""
    with log.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "worker.started" in line and f"pid={pid}" in line:
                last = line
    match = re.search(r"kinds=(\S+)", last)
    return set(match.group(1).strip('"').split(",")) if match else {KIND_PROCESS}


def enqueue_knowledge_refresh(db: Session, artifact_id: int) -> Job | None:
    """Обновить индекс и выжимку готового созвона после имени голоса. Не ставится, если такая задача уже ждёт."""
    existing = db.scalar(
        select(Job.id).where(Job.artifact_id == artifact_id, Job.kind == KIND_REFRESH, Job.status == "queued")
    )
    if existing:
        return None
    job = Job(artifact_id=artifact_id, kind=KIND_REFRESH, status="queued", phase="refresh")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def enqueue_auto_review(db: Session, artifact_id: int, *, scope: str, mode: str) -> Job | None:
    """Фоновая автопроверка артефакта локальной моделью. Дубль (такая же задача ждёт или идёт) не ставится."""
    if scope not in REVIEW_SCOPES:
        raise ValueError(f"scope: один из {', '.join(REVIEW_SCOPES)}")
    payload = json.dumps({"scope": scope, "mode": mode}, ensure_ascii=False, sort_keys=True)
    existing = db.scalar(
        select(Job.id).where(
            Job.artifact_id == artifact_id,
            Job.kind == KIND_AUTO_REVIEW,
            Job.status.in_(("queued", "running")),
            Job.payload_json == payload,
        )
    )
    if existing:
        return None
    job = Job(artifact_id=artifact_id, kind=KIND_AUTO_REVIEW, status="queued", phase="review", payload_json=payload)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
