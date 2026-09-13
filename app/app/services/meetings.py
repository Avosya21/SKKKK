from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.meeting import (
    PARTICIPANT_GUEST,
    PARTICIPANT_MEMBER,
    STATUS_CREATED,
    STATUS_ENDED,
    STATUS_LIVE,
    Meeting,
    MeetingParticipant,
)
from app.app.models.project import Project
from app.app.models.user import User
from app.app.services.meeting_tokens import hash_public_token, new_public_token, public_meeting_url
from app.app.services.project_access import require_project, staff_ui_role

GUEST_PREFIX = "guest:"
GUEST_ID_MAX_LEN = 64
GUEST_ID_RE = re.compile(r"[A-Za-z0-9-]+")
GUEST_IDENTITY_HASH_LEN = 32
GUEST_ID_INVALID = "guest_id invalid"
DISPLAY_NAME_MAX = 128


def require_meeting_staff(db: Session, user: User, project_id: int) -> Project:
    project = require_project(db, user, project_id)
    if staff_ui_role(db, user, project) not in ("admin", "manager"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return project


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _room_name() -> str:
    return "skkkk-" + secrets.token_hex(8)


def create_meeting(db: Session, settings: Settings, *, project: Project, actor: User) -> tuple[Meeting, str]:
    token = new_public_token()
    row = Meeting(
        project_id=project.id,
        created_by=actor.id,
        room_name=_room_name(),
        status=STATUS_CREATED,
        public_token_hash=hash_public_token(token),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, token


def list_meetings(db: Session, project_id: int) -> list[Meeting]:
    return list(
        db.scalars(
            select(Meeting).where(Meeting.project_id == project_id).order_by(Meeting.id.desc())
        )
    )


def get_meeting(db: Session, project_id: int, meeting_id: int) -> Meeting:
    row = db.get(Meeting, meeting_id)
    if not row or row.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    return row


def start_meeting(db: Session, meeting: Meeting) -> Meeting:
    if meeting.status == STATUS_ENDED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Meeting ended")
    meeting.status = STATUS_LIVE
    meeting.started_at = meeting.started_at or _now()
    db.commit()
    db.refresh(meeting)
    return meeting


def end_meeting(db: Session, meeting: Meeting) -> Meeting:
    meeting.status = STATUS_ENDED
    meeting.ended_at = _now()
    db.commit()
    db.refresh(meeting)
    return meeting


def revoke_link(db: Session, meeting: Meeting) -> Meeting:
    meeting.public_revoked_at = _now()
    db.commit()
    db.refresh(meeting)
    return meeting


def regenerate_link(db: Session, meeting: Meeting) -> tuple[Meeting, str]:
    token = new_public_token()
    meeting.public_token_hash = hash_public_token(token)
    meeting.public_revoked_at = None
    db.commit()
    db.refresh(meeting)
    return meeting, token


def meeting_by_token(db: Session, token: str) -> Meeting:
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    row = db.scalar(select(Meeting).where(Meeting.public_token_hash == hash_public_token(token)))
    if not row or row.public_revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    return row


def public_payload(meeting: Meeting) -> dict:
    return {
        "status": meeting.status,
        "started": meeting.status == STATUS_LIVE,
        "ended": meeting.status == STATUS_ENDED,
        "project_id": None,
    }


def member_payload(
    settings: Settings,
    meeting: Meeting,
    *,
    public_token: str | None,
    db: Session | None = None,
) -> dict:
    from app.app.services.meeting_analyze import analyze_state

    payload = {
        "id": meeting.id,
        "project_id": meeting.project_id,
        "status": meeting.status,
        "room_name": meeting.room_name,
        "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
        "started_at": meeting.started_at.isoformat() if meeting.started_at else None,
        "ended_at": meeting.ended_at.isoformat() if meeting.ended_at else None,
        "public_token": public_token,
        "public_revoked": meeting.public_revoked_at is not None,
    }
    if public_token:
        payload["public_url"] = public_meeting_url(settings, public_token)
    payload.update(analyze_state(settings, meeting, db))
    return payload


def _join_payload(settings: Settings, meeting: Meeting, identity: str, display_name: str) -> dict:
    return {
        "identity": identity,
        "display_name": display_name,
        "room_name": meeting.room_name,
        "livekit": livekit_join(settings, meeting.room_name, identity, display_name, can_publish=True),
    }


def join_member(db: Session, settings: Settings, meeting: Meeting, user: User) -> dict:
    if meeting.status != STATUS_LIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Meeting is not live")
    name = (user.name or f"user-{user.id}")[:128]
    identity = f"member:{user.id}"
    db.add(
        MeetingParticipant(
            meeting_id=meeting.id,
            user_id=user.id,
            identity=identity,
            display_name=name,
            participant_type=PARTICIPANT_MEMBER,
        )
    )
    db.commit()
    return _join_payload(settings, meeting, identity, name)


def parse_guest_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if len(value) > GUEST_ID_MAX_LEN or not GUEST_ID_RE.fullmatch(value):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GUEST_ID_INVALID)
    return value


def guest_identity(token_hash: str, guest_id: str) -> str:
    digest = hashlib.sha256(f"{token_hash}:{guest_id}".encode()).hexdigest()
    return GUEST_PREFIX + digest[:GUEST_IDENTITY_HASH_LEN]


def _upsert_guest(db: Session, meeting: Meeting, identity: str, name: str) -> None:
    row = db.scalar(
        select(MeetingParticipant).where(
            MeetingParticipant.meeting_id == meeting.id,
            MeetingParticipant.identity == identity,
        )
    )
    if row:
        row.display_name = name
        row.joined_at = _now()
        return
    db.add(
        MeetingParticipant(
            meeting_id=meeting.id,
            identity=identity,
            display_name=name,
            participant_type=PARTICIPANT_GUEST,
        )
    )


def join_guest(
    db: Session,
    settings: Settings,
    meeting: Meeting,
    display_name: str,
    guest_id: str | None = None,
) -> dict:
    name = (display_name or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="display_name required")
    if meeting.status != STATUS_LIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Meeting is not live")
    short_name = name[:DISPLAY_NAME_MAX]
    if guest_id:
        identity = guest_identity(meeting.public_token_hash, guest_id)
    else:
        identity = GUEST_PREFIX + secrets.token_hex(16)
    _upsert_guest(db, meeting, identity, short_name)
    db.commit()
    return _join_payload(settings, meeting, identity, short_name)


def livekit_join(
    settings: Settings,
    room_name: str,
    identity: str,
    display_name: str,
    *,
    can_publish: bool,
) -> dict:
    url = (settings.livekit_url or "").strip()
    if not url or not settings.livekit_api_key or not settings.livekit_api_secret:
        return {"configured": False}
    return {
        "configured": True,
        "url": url,
        "token": _livekit_jwt(settings, room_name, identity, display_name, can_publish=can_publish),
    }


def _livekit_jwt(
    settings: Settings,
    room_name: str,
    identity: str,
    display_name: str,
    *,
    can_publish: bool,
) -> str:
    from datetime import timedelta

    from jose import jwt

    now = datetime.now(timezone.utc)
    video = {
        "roomJoin": True,
        "room": room_name,
        "canSubscribe": True,
        "canPublish": can_publish,
        "canPublishData": True,
        "canUpdateOwnMetadata": True,
    }
    payload = {
        "iss": settings.livekit_api_key,
        "sub": identity,
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(hours=6)).timestamp()),
        "name": display_name,
        "video": video,
    }
    token = jwt.encode(
        payload,
        settings.livekit_api_secret,
        algorithm="HS256",
        headers={"kid": settings.livekit_api_key},
    )
    return token if isinstance(token, str) else token.decode("utf-8")
