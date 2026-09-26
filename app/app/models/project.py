"""Модель Project — проект и его настройки."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.app.db import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    settings_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="project")

    def settings(self) -> dict:
        try:
            return json.loads(self.settings_json or "{}")
        except json.JSONDecodeError:
            return {}

    def set_settings(self, data: dict) -> None:
        self.settings_json = json.dumps(data, ensure_ascii=False)
