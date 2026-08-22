from __future__ import annotations

import logging
from typing import Any

import httpx

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
