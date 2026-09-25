"""Модель AgentQuestion — вопрос внешнего агента в ящике вопросов проекта (таблица agent_questions)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class AgentQuestion(Base):
    """Входящий ящик вопросов внешнего агента. Текст вопроса — недоверенные данные.

    Статусы: pending (ждёт ответа) → drafted (черновик локальной модели, наружу не виден) → answered.
    Наружу уходит только ответ со статусом answered (автор: human | claude | local_model после одобрения)."""

    __tablename__ = "agent_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    requirement_ids_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    sources_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)  # фрагменты, найденные шлюзом по вопросу
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    draft_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_author: Mapped[str | None] = mapped_column(String(32), nullable=True)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_author: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cited_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
