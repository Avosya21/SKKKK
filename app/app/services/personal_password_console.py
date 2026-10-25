"""Сброс личного пароля из консоли: пункт 7 меню запуска `main` и `scripts/personal_password.py`.

Список — логины из `Login_up.txt` (плюс логины с записью в базе паролей, которых в списке уже нет):
есть ли личный пароль, когда менялся, последний вход.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.app.config import get_settings
from app.app.logging_setup import log_event
from app.app.services import personal_password
from app.app.services.login_allowlist import load_allowlist, login_key

logger = logging.getLogger("primaryanalitics.auth")


@dataclass(frozen=True)
class LoginRow:
    login: str
    key: str
    has_password: bool
    reset_pending: bool
    updated_at: datetime | None
    last_login: datetime | None
    in_allowlist: bool


def _last_logins() -> dict[str, datetime]:
    """Последний вход по логину (из журнала действий app.db). Без базы — пусто, список всё равно покажется."""
    try:
        from sqlalchemy import func, select

        from app.app.db import SessionLocal
        from app.app.models.user import User
        from app.app.models.user_activity_log import UserActivityLog
        from app.app.services.user_activity import ACTION_LOGIN, BLOCK_AUTH

        db = SessionLocal()
        try:
            rows = db.execute(
                select(User.name, func.max(UserActivityLog.created_at))
                .join(UserActivityLog, UserActivityLog.user_id == User.id)
                .where(UserActivityLog.block == BLOCK_AUTH, UserActivityLog.action == ACTION_LOGIN)
                .group_by(User.name)
            ).all()
        finally:
            db.close()
    except Exception:
        return {}
    out: dict[str, datetime] = {}
    for name, when in rows:
        key = login_key(name)
        if when and (key not in out or when > out[key]):
            out[key] = when
    return out


def login_rows() -> list[LoginRow]:
    allow = load_allowlist(get_settings().login_allowlist_path)
    stored = personal_password.all_info()
    last = _last_logins()
    rows: list[LoginRow] = []
    for key, login in allow.items():
        info = stored.get(key)
        rows.append(
            LoginRow(
                login=login,
                key=key,
                has_password=bool(info and info.has_password),
                reset_pending=bool(info and info.reset_pending),
                updated_at=info.updated_at if info else None,
                last_login=last.get(key),
                in_allowlist=True,
            )
        )
    for key, info in sorted(stored.items()):
        if key not in allow:
            rows.append(
                LoginRow(key, key, info.has_password, info.reset_pending, info.updated_at, last.get(key), False)
            )
    return rows


def _fmt(when: datetime | None) -> str:
    """Местное время; SQLite отдаёт UTC без пояса."""
    if not when:
        return "—"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().strftime("%Y-%m-%d %H:%M")


def print_rows(rows: list[LoginRow], out: Callable[[str], None] = print) -> None:
    width = max([len(r.login) for r in rows] + [5])
    out(f"  {'№':>3}  {'Логин':<{width}}  {'Личный пароль':<15}  {'Изменён':<16}  Последний вход")
    for i, r in enumerate(rows, 1):
        state = "есть" if r.has_password else ("сброшен" if r.reset_pending else "нет")
        note = "" if r.in_allowlist else "  (нет в Login_up.txt)"
        out(f"  {i:>3}  {r.login:<{width}}  {state:<15}  {_fmt(r.updated_at):<16}  {_fmt(r.last_login)}{note}")


def resolve(choice: str, rows: list[LoginRow]) -> LoginRow | None:
    """Номер из списка или логин (без учёта регистра)."""
    choice = (choice or "").strip()
    if choice.isdigit():
        n = int(choice)
        return rows[n - 1] if 1 <= n <= len(rows) else None
    key = login_key(choice)
    return next((r for r in rows if r.key == key), None)


def reset_login(row: LoginRow, *, source: str) -> int:
    version = personal_password.reset(row.key)
    log_event(logger, "personal.reset", name=row.login, source=source, version=version)
    return version


def interactive_reset(ask: Callable[[str], str] = input, out: Callable[[str], None] = print) -> bool:
    """Пункт 7: список → номер или логин → подтверждение «да» → сброс. True — пароль сброшен."""
    rows = login_rows()
    out("")
    if not rows:
        out("Список логинов пуст (Login_up.txt).")
        return False
    print_rows(rows, out)
    try:
        choice = ask("Номер или логин (Enter — назад): ").strip()
    except EOFError:
        return False
    if not choice:
        return False
    row = resolve(choice, rows)
    if row is None:
        out(f"Логин {choice!r} не найден.")
        return False
    if not row.has_password and not row.reset_pending:
        out(f"У {row.login} личного пароля ещё нет — сбрасывать нечего.")
        return False
    out(f"Сбросить личный пароль {row.login}? Его сессии закроются, при следующем входе он создаст новый пароль.")
    try:
        answer = ask("Сбросить? [да / Enter — отмена]: ").strip().casefold()
    except EOFError:
        return False
    if answer not in {"да", "д", "yes", "y"}:
        out("Отменено.")
        return False
    reset_login(row, source="startup_menu")
    out(f"Пароль {row.login} сброшен.")
    return True
