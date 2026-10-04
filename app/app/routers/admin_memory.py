"""Табло памяти решений /admin/memory: метрики качества, чек-лист приёмки платформы, история прогонов (ПФ10).

Доступ — как у панели пользователей: только логины из ``Panel_users.txt`` (и только администраторы), остальным 404.
Считается по базе и файлам эталона, модель не вызывается; на открытии страницы прогон делается сам, если с последнего
сменились промпт или модель (только для проектов с включённой платформой памяти).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import get_current_user, require_html_user
from app.app.models.person import Person
from app.app.models.project import Project
from app.app.models.user import User
from app.app.services import admin_panel, memory_platform
from app.app.services import memory_scorecard as sc

router = APIRouter(tags=["admin-memory"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


class RunIn(BaseModel):
    project_id: int


def _owner(user: User) -> None:
    if not admin_panel.has_access(user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None or not project.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def _payload(result: dict) -> dict:
    return {k: result[k] for k in ("project_id", "prompt", "model", "metrics", "details", "pending", "checklist", "has_gold", "has_people_sheet")}


@router.get("/admin/memory", response_class=HTMLResponse)
def memory_page(request: Request, user: Annotated[User, Depends(require_html_user)]):
    _owner(user)
    return templates.TemplateResponse(request, "admin_memory.html", {"title": "Табло памяти решений"})


@router.get("/admin/memory/data")
def memory_data(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
    project_id: Annotated[int | None, Query()] = None,
):
    """Проекты для выбора и история прогонов выбранного. Если сменились промпт или модель — прогон записывается сам."""
    _owner(user)
    people_count = dict(db.execute(select(Person.project_id, func.count()).where(Person.merged_into.is_(None)).group_by(Person.project_id)).all())
    projects = [{"id": p.id, "name": p.name, "platform": memory_platform.enabled(settings, p), "people": people_count.get(p.id, 0)}
                for p in db.scalars(select(Project).where(Project.is_active.is_(True)).order_by(Project.id.desc())).all()]
    chosen = project_id if project_id is not None else next((p["id"] for p in projects if p["platform"]), projects[0]["id"] if projects else None)
    auto = False
    if chosen is not None and any(p["id"] == chosen and p["platform"] for p in projects) and sc.changed_since_last_run(settings, chosen):
        sc.run(db, settings, chosen, stage="авто: смена промпта или модели")
        auto = True
    return {"projects": projects, "project_id": chosen, "history": sc.history(settings, chosen)[-30:][::-1] if chosen is not None else [], "auto_ran": auto}


@router.get("/admin/memory/live")
def memory_live(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
    project_id: Annotated[int, Query()],
):
    """Метрики и чек-лист «сейчас», без записи в историю."""
    _owner(user)
    _project(db, project_id)
    return _payload(sc.compute(db, settings, project_id))


@router.post("/admin/memory/run")
def memory_run(
    body: RunIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Пересчитать и записать строку в историю прогонов."""
    _owner(user)
    _project(db, body.project_id)
    result = sc.run(db, settings, body.project_id, stage="кнопка")
    return {**_payload(result), "row": result["row"]}
