"""Список компаний в портале (PLATFORM-FOUNDATION-PLAN, ПФ4): просмотр — админ и менеджер, правка — админ.

Источник истины остаётся файл ``Companies.txt``: правка из портала сначала записывает его, затем обновляет таблицу.
Страница ``/admin/companies`` — только для админа.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.db import get_db
from app.app.dependencies import get_current_user, require_html_user
from app.app.models.company import Company
from app.app.models.user import User
from app.app.models.user_profile import UserProfile
from app.app.services import companies as svc

router = APIRouter(tags=["companies"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


class CompanyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    side: str
    active: bool = True


def _admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def _payload(db: Session) -> dict:
    svc.sync_companies(db)
    counts = dict(db.execute(select(UserProfile.company_id, func.count()).group_by(UserProfile.company_id)).all())
    rows = db.scalars(select(Company).order_by(Company.active.desc(), Company.name)).all()
    return {"items": [{"id": c.id, "name": c.name, "side": c.side, "active": c.active, "people": counts.get(c.id, 0)} for c in rows]}


@router.get("/admin/companies", response_class=HTMLResponse)
def companies_page(request: Request, user: Annotated[User, Depends(require_html_user)]):
    _admin(user)
    return templates.TemplateResponse(request, "companies.html", {"title": "Компании"})


@router.get("/companies")
def list_companies(db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(get_current_user)]):
    if user.role not in ("admin", "manager"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return _payload(db)


@router.put("/companies")
def save_company(body: CompanyIn, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(get_current_user)]):
    """Добавить компанию, сменить сторону или скрыть (active=false); переименования нет: новое название — новая компания."""
    _admin(user)
    try:
        svc.save_company(db, body.name, body.side, active=body.active)
    except svc.CompanyError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _payload(db)
