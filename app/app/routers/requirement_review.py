"""Подтверждение требований контракта (карточка «Подтверждение требований» на странице проекта).

Просмотр очереди: admin и manager проекта. Действия (подтвердить, отклонить, сменить статус): только admin."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import require_role
from app.app.models.user import User
from app.app.services import requirement_review as rr
from app.app.services.project_access import require_project

router = APIRouter(tags=["requirement-review"])


@router.get("/projects/{project_id}/requirements/review")
def review_list(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
    status: str | None = None, basis: str | None = None, verification: str | None = None, side: str | None = None, q: str | None = None,
    offset: int = 0, limit: int = 50,
):
    require_project(db, user, project_id)
    result = rr.review_list(db, settings, project_id, status=status, basis=basis, verification=verification, side=side, q=q, offset=max(0, offset), limit=limit)
    return {**result, "can_edit": user.role == "admin", "statuses": list(rr.STATUSES)}


@router.post("/projects/{project_id}/auto-review/backlog")
def auto_review_backlog(
    project_id: int, body: dict,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_role("admin"))],
):
    """«Проверить моделью всё накопленное»: фоновые задачи по готовым материалам проекта — только admin."""
    from app.app.services.auto_review import BacklogError, queue_backlog

    require_project(db, user, project_id)
    try:
        return queue_backlog(db, settings, project_id, scope=str(body.get("scope") or "all"), mode=body.get("mode") or None)
    except BacklogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/projects/{project_id}/requirements/entity/{entity_id}/undo-model")
def undo_model(
    project_id: int, entity_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_role("admin"))],
):
    """«Вернуть»: отменить решение локальной модели (подтверждение или отклонение) — только admin."""
    require_project(db, user, project_id)
    try:
        return rr.undo_model(db, settings, project_id, entity_id, user=user)
    except rr.ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/projects/{project_id}/requirements/confirm")
def confirm_batch(
    project_id: int, body: dict,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_role("admin"))],
):
    require_project(db, user, project_id)
    try:
        return rr.apply_batch(db, settings, project_id, [str(x) for x in (body.get("ids") or [])], user=user)
    except rr.ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/projects/{project_id}/requirements/{requirement_id}/{action}")
def review_one(
    project_id: int, requirement_id: str, action: str, body: dict,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_role("admin"))],
):
    require_project(db, user, project_id)
    try:
        return rr.apply(db, settings, project_id, requirement_id, action, user=user, reason=body.get("reason"), status=body.get("status"))
    except rr.ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
