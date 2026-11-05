"""Глобальная версия сессий: смена паролей ролей / «разлогинить всех» инвалидирует JWT."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.portal_setting import PortalSetting

logger = logging.getLogger("primaryanalitics.session")

KEY = "session_version"


def current(db: Session | None = None) -> int:
    """Текущая версия; нет строки — 0 (все старые токены без sv тоже 0)."""
    from app.app.db import SessionLocal

    own = db is None
    session = db if db is not None else SessionLocal()
    try:
        row = session.get(PortalSetting, KEY)
        if not row:
            return 0
        try:
            return int(row.value)
        except (TypeError, ValueError):
            return 0
    finally:
        if own:
            session.close()


def bump(db: Session | None = None, *, reason: str = "") -> int:
    """Инкремент версии — все JWT со старым sv получают 401."""
    from app.app.db import SessionLocal

    own = db is None
    session = db if db is not None else SessionLocal()
    try:
        row = session.get(PortalSetting, KEY)
        if row is None:
            row = PortalSetting(key=KEY, value="0")
            session.add(row)
            session.flush()
        nxt = int(row.value or 0) + 1
        row.value = str(nxt)
        session.commit()
        logger.info("session_version.bump to=%s reason=%s", nxt, reason or "-")
        return nxt
    except Exception:
        session.rollback()
        raise
    finally:
        if own:
            session.close()
