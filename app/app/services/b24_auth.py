"""Вход через код в личку Битрикс24 (порт логики iv-cowork)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.models.login_code import B24Employee
from app.app.services import login_codes
from app.app.services.login_allowlist import login_key

logger = logging.getLogger("primaryanalitics.b24_auth")


def enabled(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return (settings.b24_auth or "off").strip().lower() == "on"


def dev_mode(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.b24_auth_dev)


def _webhook(url: str) -> str:
    return (url or "").strip().rstrip("/") + "/"


def _call(method: str, params: dict[str, Any], *, webhook: str, timeout: float = 20.0) -> dict[str, Any]:
    base = _webhook(webhook)
    if not base.startswith("https://"):
        return {"ok": False, "error": "webhook must be https"}
    url = f"{base}{method}.json"
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, data=params)
            data = r.json() if r.content else {}
    except Exception:
        logger.exception("b24.call.fail method=%s", method)
        return {"ok": False, "error": "call failed"}
    if "error" in data:
        return {"ok": False, "error": str(data.get("error_description") or data.get("error"))}
    return {"ok": True, "result": data.get("result")}


def user_by_email(db: Session, email: str, settings: Settings | None = None) -> dict[str, Any] | None:
    """Сотрудник по email: сначала кэш b24_employees, затем REST user.get."""
    settings = settings or get_settings()
    email = login_key(email)
    if "@" not in email:
        return None
    row = db.get(B24Employee, email)
    if row is not None:
        return {"id": int(row.b24_user_id), "name": row.name or "", "email": email}

    webhook = (settings.b24_webhook_user or "").strip()
    if not webhook:
        logger.warning("b24.user.no_webhook email=%s", email)
        return None

    for params in ({"FILTER[EMAIL]": email}, {"EMAIL": email, "ACTIVE": "true"}):
        res = _call("user.get", params, webhook=webhook)
        if not res["ok"]:
            logger.error("b24.user.get err=%s email=%s", res.get("error"), email)
            return None
        for u in res.get("result") or []:
            if not isinstance(u, dict) or "ID" not in u or "EMAIL" not in u:
                continue
            if login_key(str(u["EMAIL"])) != email:
                continue
            if u.get("ACTIVE") in (False, "0", 0):
                continue
            found = {
                "id": int(u["ID"]),
                "name": f"{u.get('NAME') or ''} {u.get('LAST_NAME') or ''}".strip(),
                "email": email,
            }
            db.merge(
                B24Employee(
                    email=email,
                    b24_user_id=found["id"],
                    name=found["name"],
                    source="b24",
                )
            )
            db.commit()
            return found
    logger.warning("b24.user.not_found email=%s", email)
    return None


def send_to_user(b24_user_id: int, message: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    webhook = (settings.b24_webhook_chat or "").strip()
    if not webhook:
        logger.warning("b24.send.no_hook user=%s", b24_user_id)
        return False
    res = _call(
        "im.message.add",
        {"DIALOG_ID": str(b24_user_id), "MESSAGE": message},
        webhook=webhook,
    )
    if res["ok"]:
        logger.info("b24.send.ok user=%s", b24_user_id)
        return True
    logger.error("b24.send.fail user=%s err=%s", b24_user_id, res.get("error"))
    return False


def request_code(
    db: Session,
    *,
    login: str,
    ip: str,
    settings: Settings | None = None,
) -> tuple[bool, str]:
    """Запросить код в B24. Отказы — без утечки факта наличия учётки."""
    settings = settings or get_settings()
    email = login_key(login)
    # Dev без боевой отправки: код в лог (логин может быть не email — тесты).
    if dev_mode(settings):
        issued = login_codes.issue(db, login=email, channel="b24", ip=ip, settings=settings)
        if not issued.ok:
            return False, issued.error or login_codes.GENERIC_DENY
        login_codes.remember_dev_code(email, issued.plain_code)
        logger.info("auth:code.dev channel=b24 login=%s code=%s", email, issued.plain_code)
        return True, ""

    if "@" not in email:
        return False, login_codes.GENERIC_DENY

    if not enabled(settings):
        return False, login_codes.GENERIC_DENY

    b24 = user_by_email(db, email, settings)
    if b24 is None:
        logger.info("b24.code.no_user login=%s", email)
        return False, login_codes.GENERIC_DENY

    b24_id = int(b24["id"])
    issued = login_codes.issue(
        db, login=email, channel="b24", ip=ip, b24_user_id=b24_id, settings=settings
    )
    if not issued.ok:
        return False, issued.error or login_codes.GENERIC_DENY

    text = login_codes.code_message(issued.plain_code, minutes=max(1, settings.login_code_ttl_sec // 60))
    if not send_to_user(b24_id, text, settings):
        return False, login_codes.GENERIC_DENY
    logger.info("auth:code.sent channel=b24 login=%s b24_id=%s", email, b24_id)
    return True, ""
