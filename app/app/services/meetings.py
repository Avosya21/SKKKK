"""Встречи проекта: создание, старт и завершение, идентичность гостей и сотрудников, участники и их присутствие, идущие сейчас встречи, токены комнаты LiveKit, закрытие комнаты."""

from __future__ import annotations

import hashlib
import re
import secrets
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
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
# Токен комнаты живёт дольше самой встречи, поэтому после её завершения им можно зайти снова: держим срок коротким (был 6 ч).
LIVEKIT_TOKEN_TTL_HOURS = 4
logger = logging.getLogger("skkkk.meetings")
GUEST_ID_MAX_LEN = 64
GUEST_ID_RE = re.compile(r"[A-Za-z0-9-]+")
GUEST_IDENTITY_HASH_LEN = 32
GUEST_ID_INVALID = "guest_id invalid"
DISPLAY_NAME_MAX = 128
# Участник «на связи», если не вышел и подавал признаки жизни (вход, heartbeat) за это время.
ONGOING_SEEN_MINUTES = 10


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


def ongoing_meetings(db: Session, project_id: int | None = None) -> list[tuple[Meeting, list[MeetingParticipant]]]:
    """Идущие встречи и кто в них на связи: статус live и есть участник на связи.

    Забытая «live» без людей (её не завершили кнопкой) не считается. Этим правилом пользуются
    полоска «Идёт созвон» на странице проекта и restart.ps1 (scripts/live_meetings.py)."""
    cutoff = _now() - timedelta(minutes=ONGOING_SEEN_MINUTES)
    query = select(Meeting).where(Meeting.status == STATUS_LIVE)
    if project_id is not None:
        query = query.where(Meeting.project_id == project_id)
    result = []
    for meeting in db.scalars(query.order_by(Meeting.id)).all():
        people = list(db.scalars(
            select(MeetingParticipant).where(
                MeetingParticipant.meeting_id == meeting.id,
                MeetingParticipant.left_at.is_(None),
                func.coalesce(MeetingParticipant.last_seen_at, MeetingParticipant.joined_at) >= cutoff,
            ).order_by(MeetingParticipant.joined_at, MeetingParticipant.id)
        ).all())
        if people:
            result.append((meeting, people))
    return result


def ongoing_payload(meeting: Meeting, people: list[MeetingParticipant]) -> dict:
    return {
        "id": meeting.id,
        "started_at": meeting.started_at.isoformat() if meeting.started_at else None,
        "people": [p.display_name for p in people],
        "room_url": f"/projects/{meeting.project_id}/meetings/{meeting.id}/room",
    }


def get_meeting(db: Session, project_id: int, meeting_id: int) -> Meeting:
    row = db.get(Meeting, meeting_id)
    if not row or row.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    return row


def start_meeting(db: Session, meeting: Meeting) -> Meeting:
    if meeting.status == STATUS_ENDED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Meeting ended")
    if meeting.status == STATUS_LIVE:
        return meeting
    meeting.status = STATUS_LIVE
    meeting.started_at = meeting.started_at or _now()
    db.commit()
    db.refresh(meeting)
    return meeting


def end_meeting(db: Session, meeting: Meeting) -> Meeting:
    if meeting.status == STATUS_CREATED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Meeting has not started")
    if meeting.status == STATUS_ENDED:
        return meeting
    meeting.status = STATUS_ENDED
    meeting.ended_at = meeting.ended_at or _now()
    db.commit()
    db.refresh(meeting)
    link_registry(db, meeting)
    close_livekit_room(meeting.room_name)
    return meeting


def link_registry(db: Session, meeting: Meeting) -> None:
    """Платформа памяти включена для проекта: участники комнаты -> люди проекта или «Кто это?» (ПФ7). Ошибка не мешает встрече."""
    try:
        from app.app.config import get_settings
        from app.app.models.project import Project
        from app.app.services import memory_platform, people

        if not memory_platform.enabled(get_settings(), db.get(Project, meeting.project_id)):
            return
        people.link_meeting_participants(db, meeting)
        db.commit()
    except Exception:  # noqa: BLE001 - реестр не должен ломать завершение встречи
        db.rollback()
        logging.getLogger("skkkk.meetings").warning("реестр людей по встрече %s не обновлён", meeting.id, exc_info=True)


def close_livekit_room(room_name: str) -> bool:
    """Best-effort server-side room cleanup after a meeting ends."""
    from app.app.config import get_settings

    settings = get_settings()
    if not settings.livekit_url or not settings.livekit_api_key or not settings.livekit_api_secret:
        return False
    # API — напрямую в LiveKit на этой машине. Клиент livekit-api отбрасывает путь адреса (…:777/livekit → …:777/twirp),
    # и публичный адрес через Caddy попадал в портал: пустой 404, комната не закрывалась, боты записи держали дорожки
    # до перезапуска сервера, а запись удалённой встречи возвращалась на диск после очистки.
    api_url = settings.livekit_rtc_internal or settings.livekit_url
    try:
        from livekit import api
        import asyncio

        async def _delete() -> None:
            client = api.LiveKitAPI(
                api_url,
                settings.livekit_api_key,
                settings.livekit_api_secret,
            )
            try:
                await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
            finally:
                close = getattr(client, "aclose", None) or getattr(client, "close", None)
                if close:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result

        # Свой поток со своим циклом событий: asyncio.run() в потоке, где цикл уже идёт (асинхронный обработчик, Playwright
        # в сценариях проверки), падает — и комната молча оставалась открытой.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, _delete()).result(timeout=30)
        return True
    except Exception as exc:
        # Комнаты уже нет (все вышли раньше) — цель достигнута. Только ответ LiveKit code=not_found: голый 404
        # (code=unknown) — запрос ушёл не туда, это ошибка.
        if "not_found" in str(exc).lower():
            return True
        logger.warning("livekit room cleanup failed room=%s: %s", room_name, exc)
        return False


def revoke_link(db: Session, meeting: Meeting) -> Meeting:
    if meeting.status == STATUS_ENDED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Meeting ended")
    if meeting.public_revoked_at is not None:
        return meeting
    meeting.public_revoked_at = _now()
    db.commit()
    db.refresh(meeting)
    return meeting


def regenerate_link(db: Session, meeting: Meeting) -> tuple[Meeting, str]:
    if meeting.status == STATUS_ENDED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Meeting ended")
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
    # Имя из карточки («Имя Фамилия»): его видят в плитке и в расшифровке; без карточки — логин.
    from app.app.services.user_profile import full_name, get_profile

    name = (full_name(get_profile(db, user.id)) or user.name or f"user-{user.id}")[:128]
    identity = f"member:{user.id}"
    row = db.scalar(
        select(MeetingParticipant).where(
            MeetingParticipant.meeting_id == meeting.id,
            MeetingParticipant.identity == identity,
        )
    )
    if row:
        row.display_name = name
        row.joined_at = _now()
        row.left_at = None
    else:
        db.add(MeetingParticipant(
            meeting_id=meeting.id,
            user_id=user.id,
            identity=identity,
            display_name=name,
            participant_type=PARTICIPANT_MEMBER,
        ))
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
        row.left_at = None
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


def leave_participant(db: Session, meeting: Meeting, identity: str) -> None:
    row = db.scalar(
        select(MeetingParticipant).where(
            MeetingParticipant.meeting_id == meeting.id,
            MeetingParticipant.identity == identity,
        )
    )
    if row and row.left_at is None:
        row.left_at = _now()
        db.commit()


def heartbeat_participant(db: Session, meeting: Meeting, identity: str) -> bool:
    row = db.scalar(select(MeetingParticipant).where(
        MeetingParticipant.meeting_id == meeting.id,
        MeetingParticipant.identity == identity,
    ))
    if row is None:
        return False
    row.last_seen_at = _now()
    row.left_at = None
    db.commit()
    return True


def mark_stale_participants(db: Session, meeting: Meeting, *, ttl_seconds: int = 60) -> int:
    cutoff = _now() - timedelta(seconds=max(1, ttl_seconds))
    rows = list(db.scalars(select(MeetingParticipant).where(
        MeetingParticipant.meeting_id == meeting.id,
        MeetingParticipant.left_at.is_(None),
        MeetingParticipant.last_seen_at.is_not(None),
        MeetingParticipant.last_seen_at < cutoff,
    )).all())
    for row in rows:
        row.left_at = _now()
    if rows:
        db.commit()
    return len(rows)


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
        "exp": int((now + timedelta(hours=LIVEKIT_TOKEN_TTL_HOURS)).timestamp()),
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
