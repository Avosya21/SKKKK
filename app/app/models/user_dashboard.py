"""Проекты на личной доске пользователя (таблица user_dashboard_projects)."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class UserDashboardProject(Base):
    __tablename__ = "user_dashboard_projects"
    __table_args__ = (UniqueConstraint("user_id", "project_id", name="uq_user_dashboard_user_project"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
