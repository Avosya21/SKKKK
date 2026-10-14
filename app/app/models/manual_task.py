"""Модель ManualTask — задача проекта, которую менеджер добавил вручную (таблица manual_tasks, PORTAL-ACCURACY-MANAGER-PLAN Ш4).

Извлечённые из материалов задачи живут в ``entities`` (``entity_type=task``); ручные — здесь: переизвлечение их не трогает,
в контракт агента они не уходят.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class ManualTask(Base):
    __tablename__ = "manual_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    assignee: Mapped[str | None] = mapped_column(String(200), nullable=True)
    deadline_at: Mapped[str | None] = mapped_column(String(16), nullable=True)  # ГГГГ-ММ-ДД
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
