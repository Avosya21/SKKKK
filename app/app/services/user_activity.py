"""Журнал действий пользователя (аудит).

Пишем независимо от основной транзакции (fail-open). Ленту чата для экрана
и для следующего ответа ИИ берём из `chat_messages`, не из этой таблицы.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.app.models.chat_message import ChatMessage
from app.app.models.user import User
from app.app.models.user_activity_log import UserActivityLog

# Совпадает с chat.CHAT_PENDING_TEXT — не импортируем chat.py (цикл).
_PENDING_CHAT_TEXT = "Думаю над ответом..."

logger = logging.getLogger("skkkk.activity")

BLOCK_AUTH = "auth"
BLOCK_PROJECT = "project"
BLOCK_UPLOAD = "upload"
BLOCK_CHAT = "chat"
BLOCK_TZ = "tz"
BLOCK_SPEAKERS = "speakers"
BLOCK_KNOWLEDGE = "knowledge"
BLOCK_REPORT = "report"

ACTION_LOGIN = "login"
ACTION_LOGOUT = "logout"
ACTION_PASSWORD_SET = "password_set"
ACTION_PASSWORD_CHANGE = "password_change"
ACTION_PROFILE_SET = "profile_set"
ACTION_CREATE = "create"
ACTION_DELETE = "delete"
ACTION_RENAME = "rename"
ACTION_UPLOAD = "upload"
ACTION_QUESTION = "question"
ACTION_ANSWER = "answer"
ACTION_REBUILD = "rebuild"
ACTION_ENROLL = "enroll"
ACTION_MERGE = "merge"
ACTION_CONFIRM = "confirm"
ACTION_NOTE = "note"
ACTION_REVIEW = "review"
ACTION_RESOLVE = "resolve"
ACTION_REINDEX = "reindex"
ACTION_REEXTRACT = "reextract"
ACTION_TASK_EDIT = "task_edit"  # задачи: статус, ответственный, срок, ручная задача (PORTAL-ACCURACY-MANAGER-PLAN Ш4)

BLOCK_LABELS = {
    BLOCK_AUTH: "Вход",
    BLOCK_PROJECT: "Проект",
    BLOCK_UPLOAD: "Файлы",
    BLOCK_CHAT: "Чат с ИИ",
    BLOCK_TZ: "Резюме проекта",
    BLOCK_SPEAKERS: "Спикеры",
    BLOCK_KNOWLEDGE: "Знания",
    BLOCK_REPORT: "Отчёт файла",
}

CONTENT_LIMIT = 4000
TITLE_LIMIT = 255
CHAT_DIALOGUE_BUDGET = 32000
LIST_DEFAULT_LIMIT = 80


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1] + "…"


def _dump_meta(meta: dict[str, Any] | None) -> str | None:
    if not meta:
        return None
    try:
        return json.dumps(meta, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def log_activity(
    db: Session | None,
    *,
    user_id: int,
    block: str,
    action: str,
    title: str,
    project_id: int | None = None,
    content: str | None = None,
    target_kind: str | None = None,
    target_id: str | int | None = None,
    meta: dict[str, Any] | None = None,
) -> UserActivityLog | None:
    """Записать событие. Своя сессия — не откатывает работу вызывающего."""
    try:
        from app.app.db import SessionLocal

        bind = db.get_bind() if db is not None else None
        factory = sessionmaker(bind=bind) if bind is not None else SessionLocal
        with factory() as session:
            row = UserActivityLog(
                user_id=user_id,
                project_id=project_id,
                block=block,
                action=action,
                title=_clip(title, TITLE_LIMIT) or title[:TITLE_LIMIT],
                content=_clip(content, CONTENT_LIMIT),
                target_kind=target_kind,
                target_id=None if target_id is None else str(target_id),
                meta_json=_dump_meta(meta),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row
    except Exception:
        logger.exception(
            "user_activity.write_failed user_id=%s block=%s action=%s",
            user_id,
            block,
            action,
        )
        return None


def block_label(block: str) -> str:
    return BLOCK_LABELS.get(block, block)


def _event_dict(row: UserActivityLog, user_name: str | None) -> dict[str, Any]:
    created = row.created_at
    created_iso = created.isoformat() if created else None
    return {
        "id": row.id,
        "user_id": row.user_id,
        "user_name": user_name or "",
        "project_id": row.project_id,
        "block": row.block,
        "block_label": block_label(row.block),
        "action": row.action,
        "title": row.title,
        "content": row.content,
        "target_kind": row.target_kind,
        "target_id": row.target_id,
        "created_at": created_iso,
    }


def list_activity(
    db: Session,
    *,
    project_id: int,
    user_id: int | None = None,
    limit: int = LIST_DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    q = select(UserActivityLog, User.name).join(User, User.id == UserActivityLog.user_id)
    q = q.where(UserActivityLog.project_id == project_id)
    if user_id is not None:
        q = q.where(UserActivityLog.user_id == user_id)
    q = q.order_by(UserActivityLog.id.desc()).limit(limit)
    rows = db.execute(q).all()
    return [_event_dict(row, name) for row, name in reversed(rows)]


def _completed_chat_pairs(
    rows: list[UserActivityLog],
) -> list[tuple[UserActivityLog, UserActivityLog]]:
    pairs: list[tuple[UserActivityLog, UserActivityLog]] = []
    pending_q: UserActivityLog | None = None
    for row in rows:
        if row.action == ACTION_QUESTION:
            pending_q = row
        elif row.action == ACTION_ANSWER and pending_q is not None:
            if row.content and row.content != _PENDING_CHAT_TEXT:
                pairs.append((pending_q, row))
            pending_q = None
    return pairs


_REPEAT_WORDS = re.compile(r"\w+", re.U)
REPEAT_SIMILARITY = 0.8  # доля общих слов, с которой вопрос считается повтором прежнего
REPEAT_HIDDEN = ("(ответ на этот же вопрос скрыт: материалы и правила могли измениться — ответь заново по "
                 "материалам ниже, не по памяти)")


def _question_words(text: str) -> set[str]:
    return {w for w in _REPEAT_WORDS.findall((text or "").lower().replace("ё", "е")) if len(w) > 2}


def is_repeat_question(question: str, earlier: str) -> bool:
    """Тот же вопрос, заданный ещё раз: совпадает почти весь набор слов (порядок и опечатки в мелочах не важны)."""
    a, b = _question_words(question), _question_words(earlier)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= REPEAT_SIMILARITY


def build_chat_user_memory(db: Session, *, project_id: int, user_id: int, question: str | None = None) -> str:
    """Текст для промпта: та же лента, что на экране (`chat_messages`).

    ``question`` — текущий вопрос: прошлый ответ ИИ на тот же вопрос заменяется пометкой, иначе модель копировала его
    целиком мимо материалов и новых правил (урок 16.10, пр. 28)."""
    rows = list(
        db.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.project_id == project_id,
                ChatMessage.user_id == user_id,
            )
            .order_by(ChatMessage.id.asc())
        ).all()
    )
    while rows and (
        rows[-1].waiting
        or (
            rows[-1].role == "assistant"
            and (rows[-1].content or "").strip() == _PENDING_CHAT_TEXT
        )
    ):
        rows.pop()
    if rows and rows[-1].role == "user":
        rows.pop()

    lines: list[str] = []
    after_repeat = False
    for row in rows:
        text = (row.content or "").strip()
        if not text or row.waiting or text == _PENDING_CHAT_TEXT:
            continue
        if row.role == "user":
            lines.append(f"Пользователь: {text}")
            after_repeat = bool(question) and is_repeat_question(question, text)
        elif row.role == "assistant":
            lines.append(f"ИИ: {REPEAT_HIDDEN if after_repeat else text}")
            after_repeat = False
    if not lines:
        return ""

    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        extra = len(line) + 1
        if kept and total + extra > CHAT_DIALOGUE_BUDGET:
            break
        kept.append(line)
        total += extra
    kept.reverse()
    header = (
        "Предыдущий диалог этого пользователя в этом проекте "
        "(это вся лента чата: продолжай разговор, понимай «а это?», местоимения "
        "и отсылки к прошлым ответам. Новые факты о проекте не выдумывай — "
        "их бери из материалов ниже. Если вопрос опирается на прошлый ответ — "
        "используй прошлый ответ):"
    )
    return header + "\n" + "\n".join(kept)


def backfill_chat_activity(db: Session) -> int:
    """Один раз скопировать уже сохранённую ленту чата в журнал."""
    existing = db.scalar(
        select(func.count())
        .select_from(UserActivityLog)
        .where(UserActivityLog.block == BLOCK_CHAT)
    ) or 0
    if existing:
        return 0
    messages = list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.waiting.is_(False))
            .order_by(ChatMessage.id.asc())
        ).all()
    )
    n = 0
    for msg in messages:
        if not msg.content or msg.content == _PENDING_CHAT_TEXT:
            continue
        if msg.role == "user":
            action, title = ACTION_QUESTION, "Вопрос в чате"
        elif msg.role == "assistant":
            action, title = ACTION_ANSWER, "Ответ ИИ"
        else:
            continue
        db.add(
            UserActivityLog(
                user_id=msg.user_id,
                project_id=msg.project_id,
                block=BLOCK_CHAT,
                action=action,
                title=title,
                content=_clip(msg.content, CONTENT_LIMIT),
            )
        )
        n += 1
    if n:
        db.commit()
    return n
