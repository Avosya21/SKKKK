"""Фоновые задачи API (пересборка ТЗ, переиндексация и т.п.): запись и статусы в таблице project_tasks, защита от параллельного запуска, очистка зависших задач при старте."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.project_task import ProjectTask

TASK_TZ_REBUILD = "tz_rebuild"
TASK_CALL_SUMMARY = "call_summary_rebuild"
TASK_ARTIFACT_REINDEX = "artifact_reindex"
TASK_ENTITIES_REEXTRACT = "entities_reextract"
TASK_SPEAKER_RECALC = "speaker_recalculate"
TASK_SPEAKER_ENROLL = "speaker_enroll"
TASK_CHAT_ANSWER = "chat_answer"
TASK_CARD_REBUILD = "card_rebuild"

TASK_LABELS: dict[str, str] = {
    TASK_TZ_REBUILD: "Пересборка выжимки проекта",
    TASK_CALL_SUMMARY: "Пересборка выжимки созвона",
    TASK_ARTIFACT_REINDEX: "Переиндексация материалов",
    TASK_ENTITIES_REEXTRACT: "Переизвлечение задач и сроков",
    TASK_SPEAKER_RECALC: "Пересчёт спикеров",
    TASK_SPEAKER_ENROLL: "Сохранение профиля спикера",
    TASK_CHAT_ANSWER: "Ответ ИИ в чате",
    TASK_CARD_REBUILD: "Пересборка карточки проекта",
}

USER_SCOPED_TASKS = {TASK_CHAT_ANSWER}

STALE_MINUTES: dict[str, int] = {
    TASK_TZ_REBUILD: 120,
    TASK_CALL_SUMMARY: 45,
    TASK_ARTIFACT_REINDEX: 30,
    TASK_ENTITIES_REEXTRACT: 45,
    TASK_SPEAKER_RECALC: 30,
    TASK_SPEAKER_ENROLL: 20,
    TASK_CHAT_ANSWER: 30,
}


class TaskAlreadyRunning(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _task_to_dict(task: ProjectTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "project_id": task.project_id,
        "artifact_id": task.artifact_db_id,
        "task_type": task.task_type,
        "status": task.status,
        "label": task.label,
        "user_id": task.user_id,
        "error_message": task.error_message,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }


def cleanup_stale_tasks(db: Session) -> int:
    """Пометить зависшие running-задачи как error (после сбоя/обрыва)."""
    now = _utcnow()
    rows = db.scalars(select(ProjectTask).where(ProjectTask.status == "running")).all()
    n = 0
    for row in rows:
        limit = STALE_MINUTES.get(row.task_type, 60)
        started = row.started_at or row.created_at
        if not started:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if now - started > timedelta(minutes=limit):
            row.status = "error"
            row.error_message = "Превышено время ожидания или задача прервана"
            row.finished_at = now
            n += 1
    if n:
        db.commit()
    return n


def abandon_running_api_tasks(db: Session) -> int:
    """При старте API все running project_tasks — сироты прошлого процесса."""
    now = _utcnow()
    rows = db.scalars(select(ProjectTask).where(ProjectTask.status == "running")).all()
    n = 0
    for row in rows:
        row.status = "error"
        row.error_message = "Сервер перезапущен, задача прервана"
        row.finished_at = now
        n += 1
    if n:
        db.commit()
    return n


def _running_query(
    db: Session,
    *,
    task_type: str,
    project_id: int,
    artifact_db_id: int | None,
    user_id: int | None = None,
):
    q = select(ProjectTask).where(
        ProjectTask.status == "running",
        ProjectTask.task_type == task_type,
        ProjectTask.project_id == project_id,
    )
    if artifact_db_id is None:
        q = q.where(ProjectTask.artifact_db_id.is_(None))
    else:
        q = q.where(ProjectTask.artifact_db_id == artifact_db_id)
    if task_type in USER_SCOPED_TASKS:
        if user_id is None:
            q = q.where(ProjectTask.user_id.is_(None))
        else:
            q = q.where(ProjectTask.user_id == user_id)
    return q


def get_running_task(
    db: Session,
    *,
    task_type: str,
    project_id: int,
    artifact_db_id: int | None = None,
    user_id: int | None = None,
) -> ProjectTask | None:
    cleanup_stale_tasks(db)
    return db.scalar(
        _running_query(
            db,
            task_type=task_type,
            project_id=project_id,
            artifact_db_id=artifact_db_id,
            user_id=user_id,
        )
    )


def list_active_tasks(
    db: Session,
    *,
    project_id: int | None = None,
    artifact_db_id: int | None = None,
) -> list[dict[str, Any]]:
    cleanup_stale_tasks(db)
    q = select(ProjectTask).where(ProjectTask.status == "running")
    if artifact_db_id is not None:
        q = q.where(ProjectTask.artifact_db_id == artifact_db_id)
    elif project_id is not None:
        q = q.where(ProjectTask.project_id == project_id)
    rows = db.scalars(q.order_by(ProjectTask.id.desc())).all()
    out = [_task_to_dict(r) for r in rows]
    if project_id is not None or artifact_db_id is not None:
        out += _queued_button_tasks(db, project_id=project_id, artifact_db_id=artifact_db_id)
    return out


def _queued_button_tasks(db: Session, *, project_id: int | None, artifact_db_id: int | None) -> list[dict[str, Any]]:
    """Кнопки, поставленные в очередь воркера (queue.KIND_PROJECT_TASK), — как задачи «running» для блокировки
    кнопок и карточки «Фоновая работа»; ждущие подписаны «в очереди»."""
    from app.app.models.artifact import Artifact
    from app.app.services.queue import project_task_jobs, project_task_payload

    if project_id is None:
        art = db.get(Artifact, artifact_db_id)
        if art is None:
            return []
        project_id = art.project_id
    out = []
    for job in project_task_jobs(db, project_id):
        data = project_task_payload(job)
        task_type = str(data.get("task") or "")
        if artifact_db_id is not None and job.artifact_id != artifact_db_id:
            continue
        label = TASK_LABELS.get(task_type, task_type)
        out.append({
            "id": f"job-{job.id}",
            "project_id": project_id,
            "artifact_id": job.artifact_id if task_type == "call_summary_rebuild" else None,
            "task_type": task_type,
            "status": "running",
            "label": label if job.status == "running" else f"{label} — в очереди",
            "user_id": data.get("user_id"),
            "error_message": None,
            "started_at": (job.started_at or job.created_at).isoformat() if (job.started_at or job.created_at) else None,
            "finished_at": None,
        })
    return out


def begin_task(
    db: Session,
    *,
    task_type: str,
    project_id: int,
    label: str | None = None,
    user_id: int | None = None,
    artifact_db_id: int | None = None,
) -> ProjectTask:
    cleanup_stale_tasks(db)
    if get_running_task(
        db,
        task_type=task_type,
        project_id=project_id,
        artifact_db_id=artifact_db_id,
        user_id=user_id,
    ):
        raise TaskAlreadyRunning(f"{label or TASK_LABELS.get(task_type, task_type)} уже выполняется")
    now = _utcnow()
    task = ProjectTask(
        project_id=project_id,
        artifact_db_id=artifact_db_id,
        task_type=task_type,
        status="running",
        label=label or TASK_LABELS.get(task_type, task_type),
        user_id=user_id,
        started_at=now,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def finish_task(db: Session, task: ProjectTask) -> None:
    task.status = "done"
    task.finished_at = _utcnow()
    db.commit()


def fail_task(db: Session, task: ProjectTask, message: str) -> None:
    task.status = "error"
    task.error_message = message[:2000]
    task.finished_at = _utcnow()
    db.commit()


@contextmanager
def track_project_task(
    db: Session,
    *,
    task_type: str,
    project_id: int,
    label: str | None = None,
    user_id: int | None = None,
    artifact_db_id: int | None = None,
) -> Iterator[ProjectTask]:
    task = begin_task(
        db,
        task_type=task_type,
        project_id=project_id,
        label=label,
        user_id=user_id,
        artifact_db_id=artifact_db_id,
    )
    try:
        yield task
        finish_task(db, task)
    except Exception as exc:
        fail_task(db, task, str(exc))
        raise
