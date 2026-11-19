"""Роли логина из Login_roles.txt: админ правит файл, портал читает на каждый вход.

Формат строки: ``login | role[,role...]``. Нет строки → ``manager``.
Роль ``user`` (заказчик) нельзя смешивать с admin/manager.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.app.services.login_allowlist import login_key

logger = logging.getLogger("primaryanalitics.login_roles")

DEFAULT_ROLE = "manager"
STAFF_ROLES = frozenset({"admin", "manager"})
CUSTOMER_ROLE = "user"
# Должен совпадать с VALID_ROLES в dependencies; новые роли — оба места + ROLE_LABELS.
KNOWN_ROLES = frozenset({"admin", "manager", "user"})

# Подписи для UI; новые роли — добавить сюда и в KNOWN_ROLES / VALID_ROLES.
ROLE_LABELS: dict[str, str] = {
    "admin": "Админ",
    "manager": "Менеджер",
    "user": "Заказчик",
}


class LoginRolesError(ValueError):
    """Некорректная строка в Login_roles.txt."""


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


def is_staff_role(role: str) -> bool:
    return role in STAFF_ROLES


def is_customer_role(role: str) -> bool:
    return role == CUSTOMER_ROLE


def load_roles_map(path: Path) -> dict[str, list[str]]:
    """Ключ логина (без регистра) → упорядоченный список ролей."""
    out: dict[str, list[str]] = {}
    if not path.is_file():
        return out
    text = path.read_text(encoding="utf-8-sig")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            logger.warning("login_roles.bad_line", extra={"line": lineno, "text": line[:80]})
            continue
        left, right = line.split("|", 1)
        key = login_key(left)
        if not key:
            continue
        roles: list[str] = []
        seen: set[str] = set()
        for part in right.replace(";", ",").split(","):
            role = part.strip().casefold()
            if not role:
                continue
            if role not in KNOWN_ROLES:
                logger.warning("login_roles.unknown_role", extra={"line": lineno, "role": role})
                continue
            if role not in seen:
                seen.add(role)
                roles.append(role)
        if not roles:
            continue
        if CUSTOMER_ROLE in seen and (seen & STAFF_ROLES):
            logger.error(
                "login_roles.mixed_customer_staff line=%s login=%s — строка пропущена",
                lineno,
                left.strip(),
            )
            continue
        out[key] = roles
    return out


def roles_for(login: str, path: Path) -> list[str]:
    """Доступные роли логина; без строки в файле — ``[manager]``."""
    key = login_key(login)
    if not key:
        return [DEFAULT_ROLE]
    mapped = load_roles_map(path).get(key)
    if not mapped:
        return [DEFAULT_ROLE]
    return list(mapped)


def resolve_role(login: str, requested: str | None, path: Path) -> str | None:
    """Выбранная роль, если она разрешена логину; иначе None."""
    allowed = roles_for(login, path)
    if not allowed:
        return None
    if requested is None or not str(requested).strip():
        if len(allowed) == 1:
            return allowed[0]
        return None
    want = str(requested).strip().casefold()
    if want in allowed:
        return want
    return None


def roles_payload(login: str, path: Path) -> list[dict[str, str]]:
    """Список ``{role, label}`` для формы входа."""
    return [{"role": r, "label": role_label(r)} for r in roles_for(login, path)]
