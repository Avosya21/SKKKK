"""Ключи доступа проектного MCP: выпуск, хеширование, список, отзыв, право отправки (can_submit), право на файлы (files_access) и проверка ключа при запросе."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.project_mcp_token import ProjectMcpToken

TOKEN_PREFIX = "skkkk_mcp_"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


SCOPES = ("read", "questions", "questions_verdicts")
SCOPE_TITLES = {"read": "только чтение", "questions": "чтение + вопросы", "questions_verdicts": "чтение + вопросы и вердикты"}
MAX_RATE_LIMIT = 1000
# Посылки файлов проекта (карты конфигурации, дипмапы) — данные проекта: принятые целиком читает любой ключ проекта.
# blocked — ключу файлы закрыты явно; write — ещё и приём посылок. Старое значение none (по умолчанию до 08.10.2026) = read.
FILES_ACCESS = ("blocked", "read", "write")
FILES_ACCESS_TITLES = {"blocked": "файлы закрыты", "read": "чтение файлов", "write": "приём и чтение файлов"}


def normalize_scope(scope: str | None, can_submit: bool = False) -> str:
    """Права ключа; без явного scope — по прежнему флагу can_submit."""
    if scope is None or scope == "":
        return "questions_verdicts" if can_submit else "read"
    if scope not in SCOPES:
        raise ValueError(f"Неизвестные права ключа: {scope}. Допустимо: {', '.join(SCOPES)}")
    return scope


def normalize_rate_limit(value: int | None) -> int | None:
    if value in (None, 0):
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > MAX_RATE_LIMIT:
        raise ValueError(f"Лимит обращений — целое от 1 до {MAX_RATE_LIMIT} за 10 минут или пусто")
    return value


def normalize_files_access(value: str | None) -> str:
    """Право на посылки файлов: blocked | read | write (write включает чтение); none — прежнее имя для blocked."""
    if value is None or value == "":
        return "read"
    if value == "none":
        return "blocked"
    if value not in FILES_ACCESS:
        raise ValueError(f"Неизвестное право на файлы: {value}. Допустимо: {', '.join(FILES_ACCESS)}")
    return value


def token_files_access(row) -> str:
    value = getattr(row, "files_access", None)
    return value if value in FILES_ACCESS else "read"


def token_scope(row) -> str:
    """Права ключа с учётом старых строк (scope ещё не заполнен)."""
    scope = getattr(row, "scope", None)
    if scope in SCOPES and not (scope == "read" and getattr(row, "can_submit", False)):
        return scope
    return "questions_verdicts" if getattr(row, "can_submit", False) else "read"


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def create_project_mcp_token(
    db: Session,
    *,
    project_id: int,
    label: str,
    user_id: int,
    can_submit: bool = False,
    scope: str | None = None,
    agent_name: str | None = None,
    rate_limit: int | None = None,
    files_access: str | None = None,
) -> tuple[ProjectMcpToken, str]:
    scope = normalize_scope(scope, can_submit)
    files = normalize_files_access(files_access)
    limit = normalize_rate_limit(rate_limit)
    plain = generate_token()
    row = ProjectMcpToken(
        project_id=project_id,
        label=label.strip()[:128] or "MCP ключ",
        token_hash=_hash_token(plain),
        token_prefix=plain[:16] + "…",
        created_by=user_id,
        is_active=True,
        can_submit=scope != "read",
        scope=scope,
        agent_name=(agent_name or "").strip()[:64] or None,
        rate_limit=limit,
        files_access=files,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, plain


def list_project_mcp_tokens(db: Session, *, project_id: int) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(ProjectMcpToken)
        .where(ProjectMcpToken.project_id == project_id)
        .order_by(ProjectMcpToken.id.desc())
    ).all()
    return [
        {
            "id": row.id,
            "label": row.label,
            "token_prefix": row.token_prefix,
            "is_active": row.is_active,
            "can_submit": token_scope(row) != "read",
            "scope": token_scope(row),
            "scope_title": SCOPE_TITLES[token_scope(row)],
            "agent_name": row.agent_name,
            "rate_limit": row.rate_limit,
            "files_access": token_files_access(row),
            "files_access_title": FILES_ACCESS_TITLES[token_files_access(row)],
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        }
        for row in rows
    ]


def set_project_mcp_token_submit(db: Session, *, project_id: int, token_id: int, enabled: bool) -> bool:
    row = db.get(ProjectMcpToken, token_id)
    if not row or row.project_id != project_id or not row.is_active:
        return False
    row.scope = "questions_verdicts" if enabled else "read"
    row.can_submit = bool(enabled)
    db.commit()
    return True


def set_project_mcp_token_scope(db: Session, *, project_id: int, token_id: int, scope: str,
                                agent_name: str | None = None, rate_limit: int | None = None, update_limit: bool = False,
                                files_access: str | None = None) -> bool:
    """Сменить права ключа (и при необходимости имя агента, лимит, право на файлы). Ложь — ключ не найден или отозван."""
    scope = normalize_scope(scope)
    files = normalize_files_access(files_access) if files_access is not None else None
    row = db.get(ProjectMcpToken, token_id)
    if not row or row.project_id != project_id or not row.is_active:
        return False
    row.scope = scope
    row.can_submit = scope != "read"
    if agent_name is not None:
        row.agent_name = agent_name.strip()[:64] or None
    if update_limit:
        row.rate_limit = normalize_rate_limit(rate_limit)
    if files is not None:
        row.files_access = files
    db.commit()
    return True


def revoke_project_mcp_token(db: Session, *, project_id: int, token_id: int) -> bool:
    row = db.get(ProjectMcpToken, token_id)
    if not row or row.project_id != project_id:
        return False
    row.is_active = False
    db.commit()
    return True


def authenticate_project_mcp_token(
    db: Session,
    *,
    project_id: int,
    bearer: str | None,
) -> ProjectMcpToken | None:
    if not bearer or not bearer.startswith(TOKEN_PREFIX):
        return None
    row = db.scalar(
        select(ProjectMcpToken).where(
            ProjectMcpToken.project_id == project_id,
            ProjectMcpToken.token_hash == _hash_token(bearer),
            ProjectMcpToken.is_active.is_(True),
        )
    )
    if not row:
        return None
    row.last_used_at = _utcnow()
    db.commit()
    return row
