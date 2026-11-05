"""Модель PortalSetting — ключ/значение настроек портала (session_version и др.)."""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class PortalSetting(Base):
    """Простые настройки в БД (не .env): версия сессий для отзыва JWT."""

    __tablename__ = "portal_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
