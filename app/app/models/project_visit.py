"""Модель ProjectVisit — когда человек заходил в проект (таблица project_visits, PORTAL-ACCURACY-MANAGER-PLAN Ш5).

По ней полоска «Что нового» на странице проекта считает, что изменилось с прошлого захода. Одна строка на пару
человек + проект; ``prev_seen_at`` — точка отсчёта «нового», ``dismissed_at`` — «Скрыть» до следующих изменений.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class ProjectVisit(Base):
    __tablename__ = "project_visits"
    __table_args__ = (UniqueConstraint("user_id", "project_id", name="uq_project_visits_user_project"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prev_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
