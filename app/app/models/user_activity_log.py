"""Модель UserActivityLog — журнал действий пользователей."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class UserActivityLog(Base):
    """Журнал действий логина: кто, какой блок, что сделал.

    Append-only: приложение только INSERT; SQLite-триггеры запрещают UPDATE/DELETE.
    FK на users/projects нет — чтобы purge проекта и слияние учёток не трогали журнал.
    Аудит. Ленту чата для экрана и для следующего ответа ИИ берём из
    ``chat_messages``, не из этой таблицы.
    """

    __tablename__ = "user_activity_log"
    __table_args__ = (
        Index("ix_user_activity_user_project_id", "user_id", "project_id", "id"),
        Index("ix_user_activity_project_block", "project_id", "block"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # Денормализация для аудита (MD): логин на момент события и id карточки (1:1 с users.id).
    login: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    block: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
