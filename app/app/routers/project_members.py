from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import get_current_user
from app.app.models.user import User
from app.app.services.project_members import list_members_payload, remove_member, require_grant_access, upsert_member

router = APIRouter(prefix="/projects", tags=["project-members"])


class MemberIn(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    user_id: int | None = None
    login: str | None = Field(default=None, max_length=255)


@router.get("/{project_id}/members")
def list_members(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_grant_access(db, user, project_id)
    return list_members_payload(db, project, settings)


@router.post("/{project_id}/members", status_code=status.HTTP_200_OK)
def add_member(
    project_id: int,
    body: MemberIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_grant_access(db, user, project_id)
    row = upsert_member(
        db,
        project=project,
        actor=user,
        settings=settings,
        role=body.role,
        user_id=body.user_id,
        login=body.login,
    )
    return list_members_payload(db, project, settings) | {"user_id": row.user_id, "role": row.role}


@router.delete("/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_member(
    project_id: int,
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_grant_access(db, user, project_id)
    remove_member(db, project=project, actor=user, user_id=user_id)
    return None
