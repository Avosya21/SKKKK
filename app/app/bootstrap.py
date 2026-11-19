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

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"


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
    """Дописать в `.env` SECRET_KEY, если пуст или placeholder.

    Раньше здесь же генерировались пароли ролей — больше не нужны (роли из Login_roles.txt).
    Возвращает пустой dict (совместимость с вызовами, ожидавшими ``{роль: пароль}``).
    """
    text = _read_env()
    original = text

    secret = _get(text, "SECRET_KEY")
    if len(secret) < 8 or secret == "change-me":
        text = _set(text, "SECRET_KEY", secrets.token_hex(24))

    if text != original:
        ENV_PATH.write_text(text, encoding="utf-8")
    return {}
