"""Фоновая работа по проекту: ход, перепроверка, переизвлечение, повтор ошибок, остановка (ПФ9 PLATFORM-FOUNDATION-PLAN).

Смотреть ход — админ и менеджер проекта; ставить, повторять и останавливать — админ (как прежняя кнопка «Переизвлечь требования»).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import get_current_user
from app.app.models.user import User
from app.app.services import project_jobs
from app.app.services.project_access import require_project_staff
from app.app.services.queue import KIND_AUTO_REVIEW, running_worker_kinds
from app.app.services.user_activity import ACTION_REEXTRACT, ACTION_REVIEW, BLOCK_KNOWLEDGE, log_activity

router = APIRouter(prefix="/projects", tags=["project-background"])


class SelectionIn(BaseModel):
    artifact_ids: list[int] = Field(default_factory=list, max_length=500)  # пусто — все готовые материалы проекта


def _admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Запускает и останавливает только администратор")


def _worker(settings: Settings) -> None:
    if KIND_AUTO_REVIEW not in running_worker_kinds(settings.data_dir):
        raise HTTPException(status_code=400, detail="Воркер фоновых задач не запущен или старой версии: перезапустите сервер, иначе задачи не выполнятся")


def _materials(db: Session, project_id: int) -> list[dict]:
    from app.app.services.requirements_reextract import project_materials

    return [{"id": a.id, "name": a.original_filename, "kind": "созвон" if a.file_type == "audio" else "документ"} for a in project_materials(db, project_id)]


@router.get("/{project_id}/background")
def background_progress(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    require_project_staff(db, user, project_id)
    return {**project_jobs.progress(db, project_id), "materials": _materials(db, project_id), "can_run": user.role == "admin",
            "worker_ready": KIND_AUTO_REVIEW in running_worker_kinds(settings.data_dir)}


@router.post("/{project_id}/background/review")
def start_review(
    project_id: int, body: SelectionIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Перепроверить: требования, темы, устаревшее, конфликты, выжимка — без повторного извлечения."""
    require_project_staff(db, user, project_id)
    _admin(user)
    _worker(settings)
    try:
        result = project_jobs.queue_review(db, settings, project_id, artifact_ids=body.artifact_ids)
    except project_jobs.JobsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_activity(db, user_id=user.id, project_id=project_id, block=BLOCK_KNOWLEDGE, action=ACTION_REVIEW,
                 title="Запустил перепроверку", content=f"материалов: {result['materials']}, задач: {result['jobs_queued']}", target_kind="project", target_id=project_id)
    return result


@router.post("/{project_id}/background/reextract")
def start_reextract(
    project_id: int, body: SelectionIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Переизвлечь требования из всех или выбранных материалов; решения человека переносятся."""
    require_project_staff(db, user, project_id)
    _admin(user)
    _worker(settings)
    try:
        result = project_jobs.queue_reextract(db, settings, project_id, artifact_ids=body.artifact_ids)
    except project_jobs.JobsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_activity(db, user_id=user.id, project_id=project_id, block=BLOCK_KNOWLEDGE, action=ACTION_REEXTRACT,
                 title="Запустил переизвлечение", content=f"материалов: {result['materials']}, задач: {result['jobs_queued']}", target_kind="project", target_id=project_id)
    return result


@router.post("/{project_id}/background/retry")
def retry(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    require_project_staff(db, user, project_id)
    _admin(user)
    _worker(settings)
    return {"requeued": project_jobs.retry_errors(db, project_id)}


@router.post("/{project_id}/background/cancel")
def cancel(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Убрать из очереди ждущие задачи проекта; идущая сейчас доработает."""
    require_project_staff(db, user, project_id)
    _admin(user)
    return {"cancelled": project_jobs.cancel_pending(db, project_id)}
