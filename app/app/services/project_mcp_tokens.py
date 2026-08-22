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


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def create_project_mcp_token(
    db: Session,
    *,
    project_id: int,
    label: str,
    user_id: int,
) -> tuple[ProjectMcpToken, str]:
    plain = generate_token()
    row = ProjectMcpToken(
        project_id=project_id,
        label=label.strip()[:128] or "MCP ключ",
        token_hash=_hash_token(plain),
        token_prefix=plain[:16] + "…",
        created_by=user_id,
        is_active=True,
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
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        }
        for row in rows
    ]


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
