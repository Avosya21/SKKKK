"""Карточки людей: фото (`/people/{id}/avatar`), своя карточка (`/me/profile`, `/me/avatar`), личная доска (`/me/dashboard`), настройки (`GET /me/ui`, `PUT /me/ui`) и задачи доски (`GET /me/board`, `POST /me/board/dismiss`).

Фото видят вошедшие пользователи портала; гостям созвона — отдельный адрес в `routers/meetings.py`.
Компанию и согласие на голос здесь не меняют: это делает админ (меню запуска, `scripts/user_profile.py`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.app.db import get_db
from app.app.dependencies import get_current_user
from app.app.models.company import Company
from app.app.models.user import User
from app.app.models.user_profile import UserProfile
from app.app.schemas.project import DashboardIn, DashboardOut
from app.app.services import avatars
from app.app.services import user_profile as profiles
from app.app.services.board_tasks import dismiss_task, read_board
from app.app.services.dashboard import read_dashboard, replace_dashboard
from app.app.services.ui_settings import (
    LIST_WIDTH_MAX,
    LIST_WIDTH_MIN,
    TASKS_HEIGHT_MAX,
    TASKS_HEIGHT_MIN,
    read_ui_settings,
    update_ui_settings,
)

router = APIRouter(tags=["people"])

AVATAR_CACHE = "private, max-age=31536000, immutable"
# Адрес без версии (плитка созвона знает только id): фото может смениться — кешируем ненадолго.
AVATAR_CACHE_SHORT = "private, max-age=300"


class ProfileOut(BaseModel):
    user_id: int
    login: str
    last_name: str
    first_name: str
    full_name: str
    job_title: str
    company: str
    avatar_url: str | None
    voice_consent_at: str | None
    complete: bool


class ProfileUpdate(BaseModel):
    # Лишние поля (company_id, согласие) — ошибка, а не тихое игнорирование.
    model_config = ConfigDict(extra="forbid")

    last_name: str = Field(max_length=200)
    first_name: str = Field(max_length=200)
    job_title: str = Field(max_length=300)


def iso_utc(when: datetime | None) -> str | None:
    """Время для экрана — ISO с `Z` (DECISIONS «Время»): SQLite отдаёт UTC без пояса."""
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def avatar_url(profile: UserProfile | None, size: int = 96, *, prefix: str = "/people") -> str | None:
    if profile is None or not profile.avatar_key:
        return None
    return f"{prefix}/{profile.user_id}/avatar?size={size}&v={profile.avatar_key}"


def profile_out(db: Session, user: User, profile: UserProfile | None) -> ProfileOut:
    company = db.get(Company, profile.company_id) if profile and profile.company_id else None
    return ProfileOut(
        user_id=user.id,
        login=user.name,
        last_name=profile.last_name if profile else "",
        first_name=profile.first_name if profile else "",
        full_name=profiles.full_name(profile),
        job_title=profile.job_title if profile else "",
        company=company.name if company else "",  # без стороны: она не для пользователя
        avatar_url=avatar_url(profile),
        voice_consent_at=iso_utc(profile.voice_consent_at) if profile else None,
        complete=profiles.is_complete(profile),
    )


def avatar_response(db: Session, user_id: int, size: int, version: str | None = None) -> FileResponse:
    profile = db.get(UserProfile, user_id)
    path = avatars.avatar_path(user_id, profile.avatar_key, size) if profile and profile.avatar_key else None
    if path is None or not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    cache = AVATAR_CACHE if version and version == profile.avatar_key else AVATAR_CACHE_SHORT
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": cache})


@router.get("/people/{user_id}/avatar")
def get_avatar(
    user_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    size: Annotated[int, Query()] = 96,
    v: Annotated[str | None, Query(max_length=64)] = None,
):
    return avatar_response(db, user_id, size, v)


@router.get("/me/profile", response_model=ProfileOut)
def my_profile(user: Annotated[User, Depends(get_current_user)], db: Annotated[Session, Depends(get_db)]):
    return profile_out(db, user, profiles.get_profile(db, user.id))


@router.put("/me/profile", response_model=ProfileOut)
def update_my_profile(
    body: ProfileUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        profile = profiles.update_own(
            db, user, last_name=body.last_name, first_name=body.first_name, job_title=body.job_title
        )
    except profiles.ProfileError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return profile_out(db, user, profile)


@router.put("/me/avatar", response_model=ProfileOut)
async def update_my_avatar(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    avatar: Annotated[UploadFile, File()],
):
    data = await avatar.read(avatars.MAX_BYTES + 1)
    try:
        profile = profiles.replace_avatar(db, user, data)
    except profiles.ProfileError as exc:
        code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE if len(data) > avatars.MAX_BYTES else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=str(exc)) from None
    return profile_out(db, user, profile)


@router.get("/me/dashboard", response_model=DashboardOut)
def get_my_dashboard(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    return DashboardOut(project_ids=read_dashboard(db, user))


@router.put("/me/dashboard", response_model=DashboardOut)
def put_my_dashboard(
    body: DashboardIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    return DashboardOut(project_ids=replace_dashboard(db, user, body.project_ids))


class UiSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: Literal["list", "dashboard"] | None = None
    list_width: int | None = Field(default=None, ge=LIST_WIDTH_MIN, le=LIST_WIDTH_MAX)
    tasks_height: int | None = Field(default=None, ge=TASKS_HEIGHT_MIN, le=TASKS_HEIGHT_MAX)


class UiSettingsOut(BaseModel):
    home: Literal["list", "dashboard"]
    list_width: int | None
    tasks_height: int | None


@router.get("/me/ui", response_model=UiSettingsOut)
def get_my_ui(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    home, list_width, tasks_height = read_ui_settings(db, user)
    return UiSettingsOut(home=home, list_width=list_width, tasks_height=tasks_height)


@router.put("/me/ui", response_model=UiSettingsOut)
def put_my_ui(
    body: UiSettingsIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    home, list_width, tasks_height = update_ui_settings(
        db, user, body.model_fields_set, body.home, body.list_width, body.tasks_height
    )
    return UiSettingsOut(home=home, list_width=list_width, tasks_height=tasks_height)


class BoardItem(BaseModel):
    kind: Literal["speakers", "meeting", "material"]
    ref: str
    title: str
    hint: str
    project_id: int
    project_name: str
    href: str
    at: str | None


class BoardOut(BaseModel):
    open: list[BoardItem]
    done: list[BoardItem]


class BoardDismissIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["speakers", "meeting"]
    ref: str = Field(min_length=1, max_length=64)


@router.get("/me/board", response_model=BoardOut)
def get_my_board(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    return BoardOut.model_validate(read_board(db, user))


@router.post("/me/board/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_my_board_task(
    body: BoardDismissIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    dismiss_task(db, user, body.kind, body.ref)
