"""Выдача и проверка одноразовых кодов входа (B24 / email)."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.models.login_code import LoginCode
from app.app.services.login_allowlist import login_key

logger = logging.getLogger("primaryanalitics.login_codes")

_hasher = PasswordHasher()
_dev_lock = threading.Lock()
# Только при *_DEV: последний выданный код по login_key (тесты/стенд).
_dev_last_codes: dict[str, str] = {}

GENERIC_DENY = "Не удалось отправить код. Проверьте логин или попробуйте позже."
GENERIC_BAD = "Неверный или просроченный код"


@dataclass(frozen=True)
class IssueResult:
    ok: bool
    plain_code: str = ""
    error: str = ""
    code_id: int | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ttl_sec(settings: Settings | None = None) -> int:
    return int((settings or get_settings()).login_code_ttl_sec)


def _max_attempts(settings: Settings | None = None) -> int:
    return int((settings or get_settings()).login_code_attempts)


def rate_limited(db: Session, *, login: str, ip: str, settings: Settings | None = None) -> str | None:
    """Текст отказа при превышении лимита, иначе None."""
    settings = settings or get_settings()
    key = login_key(login)
    since = _utcnow() - timedelta(hours=1)
    per_login = (
        db.query(LoginCode)
        .filter(LoginCode.login_key == key, LoginCode.created_at >= since)
        .count()
    )
    if per_login >= int(settings.login_code_rate_login_per_hour):
        return "Слишком много запросов кода. Попробуйте через час"
    per_ip = (
        db.query(LoginCode)
        .filter(LoginCode.ip == (ip or ""), LoginCode.created_at >= since)
        .count()
    )
    if per_ip >= int(settings.login_code_rate_ip_per_hour):
        return "Слишком много запросов кода с этого адреса"
    return None


def remember_dev_code(login: str, code: str) -> None:
    with _dev_lock:
        _dev_last_codes[login_key(login)] = code


def peek_dev_code(login: str) -> str | None:
    with _dev_lock:
        return _dev_last_codes.get(login_key(login))


def clear_dev_codes() -> None:
    with _dev_lock:
        _dev_last_codes.clear()


def issue(
    db: Session,
    *,
    login: str,
    channel: str,
    ip: str,
    b24_user_id: int | None = None,
    settings: Settings | None = None,
) -> IssueResult:
    """Создать код (хэш в БД). Возвращает plaintext для отправки."""
    settings = settings or get_settings()
    limited = rate_limited(db, login=login, ip=ip, settings=settings)
    if limited:
        return IssueResult(ok=False, error=limited)
    code = f"{secrets.randbelow(1_000_000):06d}"
    row = LoginCode(
        login_key=login_key(login),
        channel=channel,
        code_hash=_hasher.hash(code),
        b24_user_id=b24_user_id,
        expires_at=_utcnow() + timedelta(seconds=_ttl_sec(settings)),
        attempts=0,
        ip=ip or "",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return IssueResult(ok=True, plain_code=code, code_id=row.id)


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    error: str = ""


def verify(
    db: Session,
    *,
    login: str,
    code: str,
    settings: Settings | None = None,
) -> VerifyResult:
    settings = settings or get_settings()
    digits = "".join(c for c in (code or "") if c.isdigit())
    if len(digits) != 6:
        return VerifyResult(ok=False, error="Код состоит из 6 цифр")
    key = login_key(login)
    row = (
        db.query(LoginCode)
        .filter(LoginCode.login_key == key, LoginCode.used_at.is_(None))
        .order_by(LoginCode.id.desc())
        .first()
    )
    if row is None:
        time.sleep(0.3)
        return VerifyResult(ok=False, error=GENERIC_BAD)
    exp = row.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < _utcnow():
        time.sleep(0.3)
        return VerifyResult(ok=False, error="Код истёк, запросите новый")
    if int(row.attempts) >= _max_attempts(settings):
        time.sleep(0.3)
        return VerifyResult(ok=False, error="Попытки исчерпаны, запросите новый код")
    try:
        _hasher.verify(row.code_hash, digits)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        row.attempts = int(row.attempts) + 1
        db.commit()
        time.sleep(0.3)
        return VerifyResult(ok=False, error=GENERIC_BAD)
    row.used_at = _utcnow()
    db.commit()
    return VerifyResult(ok=True)


def code_message(plain_code: str, *, minutes: int | None = None) -> str:
    mins = minutes if minutes is not None else max(1, _ttl_sec() // 60)
    return (
        f"Appa: Primary Analitics - код для входа: {plain_code}. "
        f"Код действует {mins} мин.\n"
        f"Если вход запрашивали не вы — просто проигнорируйте это сообщение."
    )
