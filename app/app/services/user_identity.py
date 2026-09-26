"""Учётка по имени (логину). Telegram ID только хранится и обновляется."""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact
from app.app.models.chat_guidance import ChatGuidance
from app.app.models.chat_message import ChatMessage
from app.app.models.entity import Link, Resolution, UserNote
from app.app.models.project import Project
from app.app.models.project_mcp_token import ProjectMcpToken
from app.app.models.project_member import ProjectMember
from app.app.models.project_task import ProjectTask
from app.app.models.meeting import Meeting, MeetingParticipant
from app.app.models.speaker_profile import SpeakerProfile
from app.app.models.user import User
from app.app.models.user_activity_log import UserActivityLog

logger = logging.getLogger("skkkk.identity")

_USER_ID_COLUMNS: list[tuple[type, str]] = [
    (Artifact, "user_id"),
    (ChatMessage, "user_id"),
    (ChatGuidance, "user_id"),
    (UserNote, "user_id"),
    (UserActivityLog, "user_id"),
    (ProjectTask, "user_id"),
    (Project, "created_by"),
    (ProjectMember, "user_id"),
    (ProjectMember, "granted_by"),
    (ProjectMcpToken, "created_by"),
    (Link, "created_by"),
    (Resolution, "resolved_by"),
    (Meeting, "created_by"),
    (MeetingParticipant, "user_id"),
    (SpeakerProfile, "user_id"),
]
# Ссылки на users, которые переносит не общий UPDATE, а своя функция (PK user_id: одна карточка на человека).
_USER_ID_SPECIAL: set[tuple[str, str]] = {("user_profiles", "user_id")}  # user_profile.merge_profiles


def normalize_login_name(name: str) -> str:
    return (name or "").strip()


def login_name_key(name: str) -> str:
    return normalize_login_name(name).casefold()


def find_users_by_login_name(db: Session, name: str) -> list[User]:
    key = login_name_key(name)
    if not key:
        return []
    return list(
        db.scalars(select(User).where(func.lower(User.name) == key).order_by(User.id.asc())).all()
    )


def _count(db: Session, model, column: str, user_id: int) -> int:
    col = getattr(model, column)
    return int(db.scalar(select(func.count()).select_from(model).where(col == user_id)) or 0)


def pick_canonical_user(db: Session, users: list[User]) -> User:
    def score(user: User) -> tuple[int, int, int, int]:
        return (
            _count(db, ChatMessage, "user_id", user.id),
            _count(db, Artifact, "user_id", user.id),
            _count(db, UserActivityLog, "user_id", user.id),
            -user.id,
        )

    return max(users, key=score)


def _reassign_user_rows(db: Session, *, winner_id: int, loser_id: int) -> None:
    for model, column in _USER_ID_COLUMNS:
        col = getattr(model, column)
        db.execute(update(model).where(col == loser_id).values({column: winner_id}))


def merge_users_into(db: Session, winner: User, losers: list[User]) -> int:
    n = 0
    for loser in losers:
        if loser.id == winner.id:
            continue
        # Карточка — одна на человека (PK user_id), общим UPDATE её не перенести.
        from app.app.services.user_profile import merge_profiles

        merge_profiles(db, winner_id=winner.id, loser_id=loser.id)
        _reassign_user_rows(db, winner_id=winner.id, loser_id=loser.id)
        db.delete(loser)
        n += 1
        logger.info("user.merge loser=%s -> winner=%s name=%s", loser.id, winner.id, winner.name)
    if n:
        db.commit()
        db.refresh(winner)
    return n


def merge_duplicate_logins(db: Session) -> int:
    """Склеить учётки с одинаковым именем (без учёта регистра)."""
    rows = list(db.scalars(select(User).order_by(User.id.asc())).all())
    groups: dict[str, list[User]] = defaultdict(list)
    for user in rows:
        key = login_name_key(user.name)
        if key:
            groups[key].append(user)
    merged = 0
    for users in groups.values():
        if len(users) < 2:
            continue
        winner = pick_canonical_user(db, users)
        losers = [u for u in users if u.id != winner.id]
        merged += merge_users_into(db, winner, losers)
    return merged


def free_telegram_id(db: Session, telegram_id: str, *, keep_user_id: int | None = None) -> None:
    value = (telegram_id or "").strip()
    if not value:
        return
    q = select(User).where(User.telegram_id == value)
    if keep_user_id is not None:
        q = q.where(User.id != keep_user_id)
    other = db.scalar(q)
    if other is not None:
        other.telegram_id = f"unused-{other.id}"
        db.flush()


def claim_telegram_id(db: Session, user: User, telegram_id: str) -> None:
    """Записать Telegram ID на эту учётку. Если слот занят другой — освободить."""
    value = (telegram_id or "").strip()
    if not value:
        return
    free_telegram_id(db, value, keep_user_id=user.id)
    user.telegram_id = value


def ensure_login_name_unique_index() -> None:
    from app.app.db import get_engine

    engine = get_engine()
    with engine.connect() as conn:
        try:
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_name_nocase ON users(name COLLATE NOCASE)"
            )
            conn.commit()
        except Exception:
            logger.exception("users.name unique index skipped")
