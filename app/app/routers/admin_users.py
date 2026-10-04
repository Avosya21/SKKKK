"""Панель пользователей для владельца: /admin/users — кто зарегистрирован и что делал (по всем проектам).

Доступ — только логины из ``Panel_users.txt`` (и только администраторы). Остальным страница и API отвечают 404, как будто их нет.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.app.db import get_db
from app.app.dependencies import get_current_user, require_html_user
from app.app.models.user import User
from app.app.services import admin_panel

router = APIRouter(tags=["admin-users"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _owner(user: User) -> None:
    if not admin_panel.has_access(user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@router.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request, user: Annotated[User, Depends(require_html_user)]):
    _owner(user)
    return templates.TemplateResponse(request, "admin_users.html", {"title": "Пользователи"})


@router.get("/admin/users/data")
def users_data(db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(get_current_user)]):
    _owner(user)
    return {"summary": admin_panel.summary(db), "users": admin_panel.users_table(db), "filters": admin_panel.filter_options(db)}


@router.get("/admin/users/activity")
def users_activity(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    user_id: Annotated[int | None, Query()] = None,
    project_id: Annotated[int | None, Query()] = None,
    block: Annotated[str | None, Query(max_length=32)] = None,
    action: Annotated[str | None, Query(max_length=48)] = None,
    date_from: Annotated[str | None, Query(max_length=10)] = None,
    date_to: Annotated[str | None, Query(max_length=10)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    before_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=admin_panel.FEED_MAX)] = 50,
):
    _owner(user)
    return admin_panel.activity_feed(db, user_id=user_id, project_id=project_id, block=block or None, action=action or None,
                                     date_from=date_from, date_to=date_to, text=q, before_id=before_id, limit=limit)
