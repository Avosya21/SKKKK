"""Подготовка `.env` до того, как Settings прочитает файл.

Модуль намеренно не импортирует `app.app.config`: `get_settings()` закеширован
через `lru_cache`, и любая запись в `.env` после первого вызова уже не влияет на
работающий процесс.
"""

from __future__ import annotations

import re
import secrets
import shutil
from pathlib import Path

import bcrypt

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

ROLES: tuple[str, ...] = ("admin", "manager", "user")

# Без похожих друг на друга символов: 0/O, 1/l/I.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
_PASSWORD_LENGTH = 14


def hash_key(role: str) -> str:
    return f"{role.upper()}_PASSWORD_HASH"


def generate_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_PASSWORD_LENGTH))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _read_env() -> str:
    if not ENV_PATH.exists():
        if ENV_EXAMPLE.exists():
            shutil.copy2(ENV_EXAMPLE, ENV_PATH)
        else:
            ENV_PATH.write_text("", encoding="utf-8")
    return ENV_PATH.read_text(encoding="utf-8")


def _get(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}\s*=(.*)$", text, flags=re.M)
    return match.group(1).strip() if match else ""


def _set(text: str, key: str, value: str) -> str:
    pattern = rf"^{re.escape(key)}\s*=.*$"
    if re.search(pattern, text, flags=re.M):
        return re.sub(pattern, f"{key}={value}", text, count=1, flags=re.M)
    return text.rstrip("\n") + f"\n{key}={value}\n"


def ensure_env() -> dict[str, str]:
    """Дописать в `.env` то, без чего приложение не стартует.

    Возвращает `{роль: пароль}` только для ролей, пароли которых сгенерированы
    прямо сейчас. Уже заполненные хеши не трогает: восстановить из них исходный
    пароль нельзя, поэтому и показать его больше нечем.
    """
    text = _read_env()
    original = text

    secret = _get(text, "SECRET_KEY")
    if len(secret) < 8 or secret == "change-me":
        text = _set(text, "SECRET_KEY", secrets.token_hex(24))

    created: dict[str, str] = {}
    for role in ROLES:
        if _get(text, hash_key(role)):
            continue
        password = generate_password()
        text = _set(text, hash_key(role), hash_password(password))
        created[role] = password

    if text != original:
        ENV_PATH.write_text(text, encoding="utf-8")
    return created


def reset_role_passwords() -> dict[str, str]:
    """Перевыпустить пароли всех трёх ролей и вернуть `{роль: пароль}`."""
    text = _read_env()
    passwords: dict[str, str] = {}
    for role in ROLES:
        password = generate_password()
        text = _set(text, hash_key(role), hash_password(password))
        passwords[role] = password
    ENV_PATH.write_text(text, encoding="utf-8")
    return passwords


def role_hashes() -> dict[str, str]:
    text = _read_env()
    return {role: _get(text, hash_key(role)) for role in ROLES}
