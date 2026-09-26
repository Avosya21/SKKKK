"""Публичные токены встреч: генерация, хеш и адрес гостевой ссылки."""

from __future__ import annotations

import hashlib
import secrets

TOKEN_BYTES = 32


def hash_public_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_public_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def public_meeting_url(settings, token: str) -> str:
    base = (settings.web_public_url or "").rstrip("/")
    path = f"/m/{token}"
    return f"{base}{path}" if base else path
