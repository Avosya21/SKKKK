"""Личный пароль логина: хеш Argon2id, правила, создание, смена, сброс, версия для сессий.

Хранится в отдельной базе (`credentials_db`). Пароль в открытом виде никуда не пишется.
Роутеры и меню запуска вызывают только функции этого модуля.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.app.config import get_settings
from app.app.credentials_db import PersonalPassword, credentials_session
from app.app.services.login_allowlist import login_key

MAX_LEN = 128
_VERSION_TTL_SEC = 5.0

_hasher = PasswordHasher()  # Argon2id, параметры библиотеки по умолчанию
_versions: dict[str, tuple[int, float]] = {}
_versions_lock = threading.Lock()


class PasswordRuleError(ValueError):
    """Пароль не подходит по правилам; текст — для показа пользователю."""


class PasswordStateError(RuntimeError):
    """Действие не подходит к состоянию (пароль уже есть / ещё нет)."""


@dataclass(frozen=True)
class PasswordInfo:
    login_key: str
    has_password: bool
    version: int
    updated_at: datetime | None
    reset_pending: bool


def mode() -> str:
    return get_settings().personal_password


def enabled() -> bool:
    return mode() != "off"


def min_length() -> int:
    return max(1, int(get_settings().personal_password_min_len))


def clear_cache() -> None:
    with _versions_lock:
        _versions.clear()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _remember_version(key: str, version: int) -> None:
    with _versions_lock:
        _versions[key] = (version, time.monotonic())


def info(login: str) -> PasswordInfo:
    key = login_key(login)
    with credentials_session() as db:
        row = db.get(PersonalPassword, key)
        if row is None:
            return PasswordInfo(key, False, 0, None, False)
        return PasswordInfo(key, bool(row.password_hash), row.pwd_version, row.updated_at, row.reset_pending)


def has_password(login: str) -> bool:
    return info(login).has_password


def current_version(login: str) -> int:
    """Версия пароля для проверки сессии; кеш на несколько секунд (вызывается на каждом запросе).

    Сброс из меню запуска идёт в другом процессе — кеш с коротким сроком это учитывает.
    """
    key = login_key(login)
    now = time.monotonic()
    with _versions_lock:
        cached = _versions.get(key)
        if cached and now - cached[1] < _VERSION_TTL_SEC:
            return cached[0]
    version = info(key).version
    _remember_version(key, version)
    return version


def all_info() -> dict[str, PasswordInfo]:
    with credentials_session() as db:
        rows = db.query(PersonalPassword).all()
        return {
            r.login_key: PasswordInfo(r.login_key, bool(r.password_hash), r.pwd_version, r.updated_at, r.reset_pending)
            for r in rows
        }


def check_rules(new: str, confirm: str, *, login: str) -> None:
    """Проверить новый пароль; при нарушении — PasswordRuleError с текстом для пользователя."""
    new = new or ""
    if new != (confirm or ""):
        raise PasswordRuleError("Пароли не совпадают")
    if len(new) < min_length():
        raise PasswordRuleError(f"Пароль короче {min_length()} символов")
    if len(new) > MAX_LEN:
        raise PasswordRuleError(f"Пароль длиннее {MAX_LEN} символов")
    if not new.strip():
        raise PasswordRuleError("Пароль не может состоять из пробелов")
    if login_key(new) == login_key(login):
        raise PasswordRuleError("Пароль не должен совпадать с логином")
    from app.app.dependencies import authenticate_new_user

    if authenticate_new_user(get_settings(), new):
        raise PasswordRuleError("Пароль не должен совпадать с паролем роли")


def verify(login: str, plain: str) -> bool:
    key = login_key(login)
    with credentials_session() as db:
        row = db.get(PersonalPassword, key)
        if row is None or not row.password_hash:
            return False
        try:
            _hasher.verify(row.password_hash, plain or "")
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        if _hasher.check_needs_rehash(row.password_hash):
            row.password_hash = _hasher.hash(plain)
            db.commit()
        return True


def set_initial(login: str, new: str, confirm: str) -> int:
    """Первый пароль (или после сброса). Возвращает версию для токена сессии."""
    check_rules(new, confirm, login=login)
    key = login_key(login)
    with credentials_session() as db:
        row = db.get(PersonalPassword, key)
        if row is not None and row.password_hash:
            raise PasswordStateError("Личный пароль уже создан")
        if row is None:
            # Версия 1, а не 0: сессии, выданные до создания пароля (pv=0), перестают действовать.
            row = PersonalPassword(login_key=key, pwd_version=1)
            db.add(row)
        row.password_hash = _hasher.hash(new)
        row.reset_pending = False
        row.updated_at = _utcnow()
        db.commit()
        version = row.pwd_version
    _remember_version(key, version)
    return version


def change(login: str, current: str, new: str, confirm: str) -> int:
    """Смена своего пароля: текущий обязателен. Возвращает новую версию."""
    if not verify(login, current):
        raise PasswordStateError("Текущий личный пароль неверен")
    check_rules(new, confirm, login=login)
    if new == current:
        raise PasswordRuleError("Новый пароль совпадает с текущим")
    key = login_key(login)
    with credentials_session() as db:
        row = db.get(PersonalPassword, key)
        row.password_hash = _hasher.hash(new)
        row.pwd_version += 1
        row.updated_at = _utcnow()
        db.commit()
        version = row.pwd_version
    _remember_version(key, version)
    return version


def reset(login: str) -> int:
    """Сброс админом: хеш удаляется, версия растёт (строка остаётся, чтобы версия не вернулась к 0)."""
    key = login_key(login)
    with credentials_session() as db:
        row = db.get(PersonalPassword, key)
        if row is None:
            row = PersonalPassword(login_key=key, pwd_version=0)
            db.add(row)
        row.password_hash = ""
        row.pwd_version += 1
        row.reset_pending = True
        row.updated_at = _utcnow()
        db.commit()
        version = row.pwd_version
    _remember_version(key, version)
    return version
