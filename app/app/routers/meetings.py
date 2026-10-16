"""Встречи проекта и гостевой вход: создание, старт, завершение, отзыв ссылки, вход и выход гостя, присутствие."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import get_current_user
from app.app.models.user import User
from app.app.services.meeting_analyze import analyze_meeting
from app.app.services.project_purge import purge_meeting
from app.app.services.user_activity import ACTION_DELETE, BLOCK_PROJECT, log_activity
from app.app.services.meetings import (
    create_meeting,
    end_meeting,
    get_meeting,
    join_guest,
    join_member,
    leave_participant,
    heartbeat_participant,
    list_meetings,
    meeting_by_token,
    member_payload,
    ongoing_meetings,
    ongoing_payload,
    parse_guest_id,
    public_payload,
    regenerate_link,
    require_meeting_staff,
    revoke_link,
    start_meeting,
)

router = APIRouter(tags=["meetings"])

MEETING_DELETE_TITLE = "Удалил созвон"


class GuestJoinIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    guest_id: str | None = None
    company_name: str | None = Field(default=None, max_length=200)  # Р7: компания гостя (из сторон проекта)


class LeaveIn(BaseModel):
    identity: str = Field(min_length=1, max_length=128)


def _staff_meeting(db: Session, user: User, project_id: int, meeting_id: int):
    require_meeting_staff(db, user, project_id)
    return get_meeting(db, project_id, meeting_id)


@router.post("/projects/{project_id}/meetings")
def create_project_meeting(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_meeting_staff(db, user, project_id)
    row, token = create_meeting(db, settings, project=project, actor=user)
    return member_payload(settings, row, public_token=token, db=db)


@router.get("/projects/{project_id}/meetings")
def list_project_meetings(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    require_meeting_staff(db, user, project_id)
    return [member_payload(settings, row, public_token=None, db=db) for row in list_meetings(db, project_id)]


# Объявлен до /meetings/{meeting_id}: иначе «live» попадёт в meeting_id и запрос получит 422.
@router.get("/projects/{project_id}/meetings/live")
def live_project_meetings(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Идущие сейчас встречи проекта для полоски «Идёт созвон»: кто в комнате и адрес входа."""
    require_meeting_staff(db, user, project_id)
    return [ongoing_payload(meeting, people) for meeting, people in ongoing_meetings(db, project_id)]


@router.get("/projects/{project_id}/meetings/{meeting_id}")
def get_project_meeting(
    project_id: int,
    meeting_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    meeting = _staff_meeting(db, user, project_id, meeting_id)
    return member_payload(settings, meeting, public_token=None, db=db)


@router.post("/projects/{project_id}/meetings/{meeting_id}/start")
def start_project_meeting(
    project_id: int,
    meeting_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    meeting = _staff_meeting(db, user, project_id, meeting_id)
    return member_payload(settings, start_meeting(db, meeting), public_token=None, db=db)


@router.post("/projects/{project_id}/meetings/{meeting_id}/end")
def end_project_meeting(
    project_id: int,
    meeting_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    meeting = _staff_meeting(db, user, project_id, meeting_id)
    return member_payload(settings, end_meeting(db, meeting), public_token=None, db=db)


@router.post("/projects/{project_id}/meetings/{meeting_id}/revoke-link")
def revoke_project_meeting_link(
    project_id: int,
    meeting_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    meeting = _staff_meeting(db, user, project_id, meeting_id)
    return member_payload(settings, revoke_link(db, meeting), public_token=None, db=db)


@router.post("/projects/{project_id}/meetings/{meeting_id}/join")
def join_project_meeting(
    project_id: int,
    meeting_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    meeting = _staff_meeting(db, user, project_id, meeting_id)
    return join_member(db, settings, meeting, user)


@router.post("/projects/{project_id}/meetings/{meeting_id}/regenerate-link")
def regenerate_project_meeting_link(
    project_id: int,
    meeting_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    meeting = _staff_meeting(db, user, project_id, meeting_id)
    row, token = regenerate_link(db, meeting)
    return member_payload(settings, row, public_token=token, db=db)


@router.delete("/projects/{project_id}/meetings/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project_meeting(
    project_id: int,
    meeting_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    meeting = _staff_meeting(db, user, project_id, meeting_id)
    room = meeting.room_name
    purge_meeting(db, settings, meeting)
    log_activity(
        db,
        user_id=user.id,
        project_id=project_id,
        block=BLOCK_PROJECT,
        action=ACTION_DELETE,
        title=MEETING_DELETE_TITLE,
        content=f"{room} (id={meeting_id})",
        target_kind="meeting",
        target_id=str(meeting_id),
    )


def _own_member_identity(user: User, identity: str) -> None:
    if identity != f"member:{user.id}":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


@router.post("/projects/{project_id}/meetings/{meeting_id}/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
def staff_meeting_heartbeat(
    project_id: int,
    meeting_id: int,
    body: LeaveIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    meeting = _staff_meeting(db, user, project_id, meeting_id)
    _own_member_identity(user, body.identity)
    if not heartbeat_participant(db, meeting, body.identity):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Participant not found")


@router.post("/projects/{project_id}/meetings/{meeting_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def staff_meeting_leave(
    project_id: int,
    meeting_id: int,
    body: LeaveIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    meeting = _staff_meeting(db, user, project_id, meeting_id)
    _own_member_identity(user, body.identity)
    leave_participant(db, meeting, body.identity)


@router.post("/projects/{project_id}/meetings/{meeting_id}/analyze")
def analyze_project_meeting(
    project_id: int,
    meeting_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    meeting = _staff_meeting(db, user, project_id, meeting_id)
    return member_payload(settings, analyze_meeting(db, settings, meeting), public_token=None, db=db)


@router.get("/api/meetings/public/{token}")
def public_meeting_info(
    token: str,
    db: Annotated[Session, Depends(get_db)],
):
    return public_payload(meeting_by_token(db, token))


@router.get("/api/meetings/public/{token}/avatars/{user_id}")
def public_meeting_avatar(
    token: str,
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
    size: int = 96,
):
    """Фото сотрудника для гостевой страницы созвона — только если он участник этой встречи."""
    from sqlalchemy import select

    from app.app.models.meeting import MeetingParticipant
    from app.app.routers.people import avatar_response

    meeting = meeting_by_token(db, token)
    member = db.scalar(
        select(MeetingParticipant.id).where(
            MeetingParticipant.meeting_id == meeting.id,
            MeetingParticipant.identity == f"member:{user_id}",
        )
    )
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return avatar_response(db, user_id, size)


@router.post("/api/meetings/public/{token}/join")
def public_meeting_join(
    token: str,
    body: GuestJoinIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    meeting = meeting_by_token(db, token)
    return join_guest(db, settings, meeting, body.display_name, guest_id=parse_guest_id(body.guest_id),
                      company_name=body.company_name)


@router.post("/api/meetings/public/{token}/leave", status_code=status.HTTP_204_NO_CONTENT)
def public_meeting_leave(token: str, body: LeaveIn, db: Annotated[Session, Depends(get_db)]):
    meeting = meeting_by_token(db, token)
    if not body.identity.startswith("guest:"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    leave_participant(db, meeting, body.identity)


@router.post("/api/meetings/public/{token}/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
def public_meeting_heartbeat(token: str, body: LeaveIn, db: Annotated[Session, Depends(get_db)]):
    meeting = meeting_by_token(db, token)
    if not body.identity.startswith("guest:"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    if not heartbeat_participant(db, meeting, body.identity):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Participant not found")
