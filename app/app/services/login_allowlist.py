"""Допуск на вход: только логины из Login_up.txt, без учёта регистра."""

from __future__ import annotations

from pathlib import Path


def login_key(value: str) -> str:
    return (value or "").strip().casefold()


def load_allowlist(path: Path) -> dict[str, str]:
    """Ключ (без регистра) → написание из файла. Файл читается каждый раз."""
    entries: dict[str, str] = {}
    if not path.is_file():
        return entries
    text = path.read_text(encoding="utf-8-sig")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key = login_key(line)
        if key and key not in entries:
            entries[key] = line
    return entries


def resolve_allowed_login(name: str, path: Path) -> str | None:
    """Канонический логин из списка или None, если имени нет / файл пуст."""
    key = login_key(name)
    if not key:
        return None
    return load_allowlist(path).get(key)
