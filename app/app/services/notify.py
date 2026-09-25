"""Уведомления в Telegram через HTTPS Bot API: о готовности файла и событиях для пользователя."""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from app.app.config import Settings

logger = logging.getLogger("skkkk.notify")


def send_telegram(settings: Settings, chat_id: str, text: str) -> bool:
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        logger.info("telegram stub chat_id=%s text=%s", chat_id, text[:200])
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json={"chat_id": chat_id, "text": text})
            r.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("telegram send failed: %s", exc)
        return False


def notify_artifact_user(db: Session, settings: Settings, artifact, text: str) -> bool:
    """Send Telegram to users.telegram_id, not the SQLite user PK."""
    from app.app.models.user import User

    user = db.get(User, artifact.user_id)
    chat_id = (getattr(user, "telegram_id", None) or "").strip() if user else ""
    if not chat_id:
        logger.info("telegram skip: no telegram_id for user_id=%s", artifact.user_id)
        return False
    body = chat_id[1:] if chat_id.startswith("-") else chat_id
    if not body.isdigit():
        logger.warning("telegram skip: telegram_id is not a chat id")
        return False
    return send_telegram(settings, chat_id, text)
