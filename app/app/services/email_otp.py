"""Код входа на email: роль user и запасной канал для сотрудников."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.services import login_codes
from app.app.services.login_allowlist import login_key

logger = logging.getLogger("primaryanalitics.email_otp")


def enabled(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return (settings.email_otp or "off").strip().lower() == "on"


def dev_mode(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.email_otp_dev)


def smtp_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool((settings.smtp_host or "").strip() and (settings.smtp_from or "").strip())


def send_email(to_addr: str, subject: str, body: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    host = (settings.smtp_host or "").strip()
    from_addr = (settings.smtp_from or "").strip()
    if not host or not from_addr:
        logger.warning("email.send.no_smtp to=%s", to_addr)
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    # Display-name со спецсимволами (например «Appa: …») без кавычек Mail.ru
    # отвергает как «From не совпадает с пользователем».
    display = (settings.smtp_from_name or "").strip()
    if display:
        safe = display.replace("\\", "\\\\").replace('"', '\\"')
        msg["From"] = f'"{safe}" <{from_addr}>'
    else:
        msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        port = int(settings.smtp_port or 587)
        if settings.smtp_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
                if (settings.smtp_user or "").strip():
                    smtp.login(settings.smtp_user, settings.smtp_password or "")
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                if settings.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if (settings.smtp_user or "").strip():
                    smtp.login(settings.smtp_user, settings.smtp_password or "")
                smtp.send_message(msg)
        logger.info("email.send.ok to=%s", to_addr)
        return True
    except Exception:
        logger.exception("email.send.fail to=%s", to_addr)
        return False


def request_code(
    db: Session,
    *,
    login: str,
    ip: str,
    settings: Settings | None = None,
) -> tuple[bool, str]:
    """Код только на точный email-логин из allowlist (вызывающий уже проверил допуск)."""
    settings = settings or get_settings()
    email = login_key(login)
    if not enabled(settings) and not dev_mode(settings):
        return False, login_codes.GENERIC_DENY

    # Боевая отправка — только на email-логин; в DEV допускаем тестовые логины без @.
    if not dev_mode(settings) and "@" not in email:
        return False, login_codes.GENERIC_DENY

    issued = login_codes.issue(db, login=email, channel="email", ip=ip, settings=settings)
    if not issued.ok:
        return False, issued.error or login_codes.GENERIC_DENY

    body = login_codes.code_message(issued.plain_code, minutes=max(1, settings.login_code_ttl_sec // 60))
    subject = "Appa: Primary Analitics — код для входа"

    if dev_mode(settings):
        login_codes.remember_dev_code(email, issued.plain_code)
        logger.info("auth:code.dev channel=email login=%s code=%s", email, issued.plain_code)
        return True, ""

    if not send_email(email, subject, body, settings):
        return False, login_codes.GENERIC_DENY
    logger.info("auth:code.sent channel=email login=%s", email)
    return True, ""
