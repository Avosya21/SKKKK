"""Очередь задач (таблица jobs): захват следующей задачи, завершение, возврат «осиротевших», постановка обработки и повтора этапа знаний."""

from __future__ import annotations

from datetime import datetime, timezone

import json
import re
from pathlib import Path

from sqlalchemy import case, func, select, text, update
from sqlalchemy.orm import Session

from app.app.models.job import Job

KIND_PROCESS = "process_artifact"
KIND_AUTO_REVIEW = "auto_review"
KIND_REFRESH = "refresh_knowledge"  # после имени голоса у готового созвона: переиндексация чанков и выжимка
# Кнопки портала, которым нужна модель (пересборки, переизвлечение): выполняет воркер по очереди, а не процесс API —
# у API своя полоса модели только для чата (docs/plans/LLM-LANES-PLAN.md). Задача привязана к материалу проекта.
KIND_PROJECT_TASK = "project_task"
PROJECT_TASK_TZ = "tz_rebuild"
PROJECT_TASK_ENTITIES = "entities_reextract"
PROJECT_TASK_CARD = "card_rebuild"
PROJECT_TASK_SUMMARY = "call_summary_rebuild"
PROJECT_TASKS = (PROJECT_TASK_TZ, PROJECT_TASK_ENTITIES, PROJECT_TASK_CARD, PROJECT_TASK_SUMMARY)
WORKER_KINDS = (KIND_PROCESS, KIND_AUTO_REVIEW, KIND_REFRESH, KIND_PROJECT_TASK)
REVIEW_SCOPES = ("reextract", "artifact", "topics", "relevance", "conflicts", "summary", "memory", "memory_judge")


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


def finish_claimed_job(db: Session, job_pk: int, artifact_pk: int, *, ok: bool, error: str | None = None) -> bool:
    """Закрыть задачу, взятую воркером. SQLite отдаёт номер удалённой строки новой: если встречу удалили
    посреди задачи, тот же id может уже принадлежать чужой задаче — её не трогаем."""
    res = db.execute(
        update(Job)
        .where(Job.id == job_pk, Job.artifact_id == artifact_pk, Job.status == "running")
        .values(status="done" if ok else "error", finished_at=datetime.now(timezone.utc), error_message=error)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return res.rowcount == 1


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
        return set(WORKER_KINDS)
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


def project_task_payload(job: Job) -> dict:
    try:
        data = json.loads(job.payload_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def enqueue_project_task(
    db: Session, *, artifact_id: int, project_id: int, task: str, user_id: int | None, options: dict | None = None
) -> Job | None:
    """Поставить кнопку портала в очередь воркера. Такая же задача проекта уже ждёт или идёт — None."""
    if task not in PROJECT_TASKS:
        raise ValueError(f"task: один из {', '.join(PROJECT_TASKS)}")
    for row in db.scalars(
        select(Job).where(Job.kind == KIND_PROJECT_TASK, Job.status.in_(("queued", "running")))
    ).all():
        data = project_task_payload(row)
        if data.get("task") == task and data.get("project_id") == project_id and (
            task not in (PROJECT_TASK_SUMMARY,) or row.artifact_id == artifact_id
        ):
            return None
    payload = {"task": task, "project_id": project_id, "user_id": user_id, **(options or {})}
    job = Job(
        artifact_id=artifact_id,
        kind=KIND_PROJECT_TASK,
        status="queued",
        phase="project",
        payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def project_task_jobs(db: Session, project_id: int) -> list[Job]:
    """Ждущие и идущие кнопки проекта (для карточки «Фоновая работа» и блокировки кнопок)."""
    rows = db.scalars(
        select(Job).where(Job.kind == KIND_PROJECT_TASK, Job.status.in_(("queued", "running"))).order_by(Job.id)
    ).all()
    return [r for r in rows if project_task_payload(r).get("project_id") == project_id]


class ProjectTaskRefused(RuntimeError):
    """Кнопку нельзя поставить в очередь: status — HTTP-код ответа, сообщение — для человека."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def queue_project_button(
    db: Session,
    data_dir: Path,
    *,
    project_id: int,
    task: str,
    user_id: int | None,
    artifact_id: int | None = None,
    options: dict | None = None,
    empty_ok: bool = False,
) -> dict:
    """Кнопка портала → задача воркера. Без материала задача привязывается к последнему материалу проекта.
    ``empty_ok`` — в проекте без материалов ответить «нечего делать», а не ошибкой."""
    from app.app.models.artifact import Artifact
    from app.app.models.project_task import ProjectTask

    if KIND_PROJECT_TASK not in running_worker_kinds(data_dir):
        raise ProjectTaskRefused(503, "Запущен воркер старой версии — перезапустите PrimaryAnalitics, чтобы кнопка встала в очередь")
    running = db.scalar(
        select(ProjectTask.id).where(
            ProjectTask.status == "running", ProjectTask.task_type == task, ProjectTask.project_id == project_id,
            *([ProjectTask.artifact_db_id == artifact_id] if artifact_id is not None else []),
        ).limit(1)
    )
    if running:
        raise ProjectTaskRefused(409, "Эта задача уже выполняется")
    if artifact_id is None:
        artifact_id = db.scalar(
            select(Artifact.id).where(Artifact.project_id == project_id).order_by(Artifact.id.desc()).limit(1)
        )
        if artifact_id is None:
            if empty_ok:
                return {"queued": False, "detail": "В проекте нет материалов — обрабатывать нечего"}
            raise ProjectTaskRefused(400, "В проекте нет материалов")
    job = enqueue_project_task(
        db, artifact_id=artifact_id, project_id=project_id, task=task, user_id=user_id, options=options
    )
    if job is None:
        raise ProjectTaskRefused(409, "Эта задача уже в очереди или выполняется")
    ahead = db.scalar(
        select(func.count()).select_from(Job).where(Job.status.in_(("queued", "running")), Job.id < job.id)
    ) or 0
    return {"queued": True, "job_id": job.id, "ahead": ahead, "detail": "Поставлено в очередь обработки"}
