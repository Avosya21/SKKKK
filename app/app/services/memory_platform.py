"""Флаг платформы памяти решений (docs/plans/PLATFORM-FOUNDATION-PLAN.md, ПФ0).

Включено — реестр людей и стороны по проекту читаются из новых таблиц; выключено — прежняя логика сторон
(словарь имён из ролей участников). Настройка проекта ``memory_platform`` главнее общей ``MEMORY_PLATFORM`` из .env.
"""
from __future__ import annotations

from typing import Any

from app.app.config import Settings

_TRUE = {"1", "true", "yes", "on", "да"}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE


def enabled(settings: Settings, project: Any | None = None) -> bool:
    """Включена ли платформа для проекта: настройка проекта главнее .env; ошибки чтения настроек — как «выключено»."""
    value: Any = settings.memory_platform
    if project is not None:
        try:
            own = (project.settings() or {}).get("memory_platform")
        except (AttributeError, ValueError, TypeError):
            own = None
        if own is not None:
            value = own
    return _as_bool(value)
