"""Панель пользователей для владельца (/admin/users): кто зарегистрирован и что делал.

Доступ — не по роли, а по отдельному списку логинов в файле ``Panel_users.txt`` (как ``Login_up.txt``: по строке на логин,
регистр не важен, ``#`` — комментарий). Файла нет или он пуст — доступа нет ни у кого. Учётка из списка при этом должна
оставаться администратором. Читается при каждом запросе: правка файла действует сразу, без перезапуска.
Журнал — ``user_activity_log``; «последний вход» и счётчики считаются по нему.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.config import get_settings
from app.app.models.company import Company
from app.app.models.project import Project
from app.app.models.project_member import ProjectMember
from app.app.models.speaker_profile import SpeakerProfile
from app.app.models.user import User
from app.app.models.user_activity_log import UserActivityLog
from app.app.models.user_profile import UserProfile
from app.app.services.user_activity import ACTION_LOGIN, BLOCK_AUTH, BLOCK_LABELS, block_label

FEED_MAX = 200
CONTENT_PREVIEW = 600


def _key(name: str | None) -> str:
    return " ".join((name or "").split()).casefold()


def panel_logins(path: Path | None = None) -> set[str]:
    """Допущенные логины панели (ключи без регистра). Нет файла — пустое множество."""
    path = path or get_settings().panel_users_path
    if not path.is_file():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.add(_key(line))
    return out


def has_access(user: User | None, path: Path | None = None) -> bool:
    """Логин в списке панели и учётка по-прежнему администратор."""
    return bool(user is not None and user.role == "admin" and _key(user.name) in panel_logins(path))


def _iso(value: datetime | None) -> str | None:
    """Время из SQLite хранится как UTC без пояса: помечаем его, чтобы браузер показал местное."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def users_table(db: Session) -> list[dict[str, Any]]:
    """Все учётки: роль, карточка, согласие на голос, проекты, последний вход и счётчики действий."""
    users = db.scalars(select(User).order_by(User.id)).all()
    profiles = {p.user_id: p for p in db.scalars(select(UserProfile)).all()}
    companies = {c.id: c.name for c in db.scalars(select(Company)).all()}
    members: dict[int, int] = dict(db.execute(select(ProjectMember.user_id, func.count()).group_by(ProjectMember.user_id)).all())
    owned: dict[int, int] = dict(db.execute(select(Project.created_by, func.count()).where(Project.is_active.is_(True)).group_by(Project.created_by)).all())
    voices: dict[int, int] = dict(db.execute(select(SpeakerProfile.user_id, func.count()).where(SpeakerProfile.user_id.is_not(None))
                                             .group_by(SpeakerProfile.user_id)).all())
    totals: dict[int, int] = dict(db.execute(select(UserActivityLog.user_id, func.count()).group_by(UserActivityLog.user_id)).all())
    last_action: dict[int, datetime] = dict(db.execute(select(UserActivityLog.user_id, func.max(UserActivityLog.created_at))
                                                       .group_by(UserActivityLog.user_id)).all())
    logins = db.execute(select(UserActivityLog.user_id, func.count(), func.max(UserActivityLog.created_at))
                        .where(UserActivityLog.block == BLOCK_AUTH, UserActivityLog.action == ACTION_LOGIN)
                        .group_by(UserActivityLog.user_id)).all()
    login_count = {uid: n for uid, n, _ in logins}
    last_login = {uid: at for uid, _, at in logins}
    out = []
    for user in users:
        card = profiles.get(user.id)
        full = f"{card.last_name} {card.first_name}".strip() if card else ""
        out.append({
            "id": user.id, "login": user.name, "role": user.role, "registered_at": _iso(user.created_at),
            "full_name": full, "company": companies.get(card.company_id, "") if card and card.company_id else "",
            "job_title": card.job_title if card else "", "card_done": bool(card and card.completed_at),
            "voice_consent": bool(card and card.voice_consent_at), "voices": voices.get(user.id, 0),
            "projects_member": members.get(user.id, 0), "projects_owned": owned.get(user.id, 0),
            "logins": login_count.get(user.id, 0), "last_login": _iso(last_login.get(user.id)),
            "actions": totals.get(user.id, 0), "last_action": _iso(last_action.get(user.id)),
        })
    return out


def _parse_day(raw: str | None, *, end: bool) -> datetime | None:
    if not raw:
        return None
    try:
        day = datetime.strptime(raw.strip(), "%Y-%m-%d")
    except ValueError:
        return None
    return day.replace(hour=23, minute=59, second=59) if end else day


def activity_feed(db: Session, *, user_id: int | None = None, project_id: int | None = None, block: str | None = None,
                  action: str | None = None, date_from: str | None = None, date_to: str | None = None, text: str | None = None,
                  before_id: int | None = None, limit: int = 50) -> dict[str, Any]:
    """Лента действий по всем проектам, новые сверху. Курсор — ``before_id`` (id последней показанной записи)."""
    limit = max(1, min(int(limit or 50), FEED_MAX))
    q = (select(UserActivityLog, User.name, Project.name)
         .join(User, User.id == UserActivityLog.user_id)
         .outerjoin(Project, Project.id == UserActivityLog.project_id))
    if user_id is not None:
        q = q.where(UserActivityLog.user_id == user_id)
    if project_id is not None:
        q = q.where(UserActivityLog.project_id == project_id)
    if block:
        q = q.where(UserActivityLog.block == block)
    if action:
        q = q.where(UserActivityLog.action == action)
    start, end = _parse_day(date_from, end=False), _parse_day(date_to, end=True)
    if start is not None:
        q = q.where(UserActivityLog.created_at >= start)
    if end is not None:
        q = q.where(UserActivityLog.created_at <= end)
    if text and text.strip():
        like = f"%{text.strip()}%"
        q = q.where(UserActivityLog.title.ilike(like) | UserActivityLog.content.ilike(like))
    if before_id is not None:
        q = q.where(UserActivityLog.id < before_id)
    rows = db.execute(q.order_by(UserActivityLog.id.desc()).limit(limit + 1)).all()
    more = len(rows) > limit
    items = []
    for row, user_name, project_name in rows[:limit]:
        content = (row.content or "").strip()
        items.append({
            "id": row.id, "at": _iso(row.created_at), "user_id": row.user_id, "user": user_name,
            "project_id": row.project_id, "project": project_name or "", "block": row.block, "block_label": block_label(row.block),
            "action": row.action, "title": row.title, "content": content[:CONTENT_PREVIEW] + ("…" if len(content) > CONTENT_PREVIEW else ""),
            "target": f"{row.target_kind or ''} {row.target_id or ''}".strip(),
        })
    return {"items": items, "more": more, "next_before_id": items[-1]["id"] if items and more else None}


def filter_options(db: Session) -> dict[str, Any]:
    """Значения для выпадающих списков фильтров: блоки, действия, проекты с записями в журнале."""
    actions = [a for (a,) in db.execute(select(UserActivityLog.action).distinct().order_by(UserActivityLog.action)).all()]
    project_ids = [p for (p,) in db.execute(select(UserActivityLog.project_id).where(UserActivityLog.project_id.is_not(None)).distinct()).all()]
    projects = db.execute(select(Project.id, Project.name).where(Project.id.in_(project_ids or [-1])).order_by(Project.id.desc())).all()
    return {"blocks": [{"id": k, "label": v} for k, v in BLOCK_LABELS.items()], "actions": actions,
            "projects": [{"id": pid, "name": name} for pid, name in projects]}


def summary(db: Session) -> dict[str, Any]:
    """Короткая сводка над таблицей: сколько учёток, сколько с заполненной карточкой, активность за 7 суток."""
    total = db.scalar(select(func.count()).select_from(User)) or 0
    done = db.scalar(select(func.count()).select_from(UserProfile).where(UserProfile.completed_at.is_not(None))) or 0
    week = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    active = db.scalar(select(func.count(func.distinct(UserActivityLog.user_id))).where(UserActivityLog.created_at >= week)) or 0
    return {"users": total, "cards_done": done, "active_7d": active}

