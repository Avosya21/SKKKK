"""Модель AgentExchangeLog — журнал обмена с внешним агентом (таблица agent_exchange_log)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class AgentExchangeLog(Base):
    """Журнал обмена с внешним агентом: каждый запрос и каждый ответ проходят через шлюз и оставляют здесь след.

    Решения: accepted (выполнено в рамках белого списка), rejected (отклонено), needs_human (ждёт человека)."""

    __tablename__ = "agent_exchange_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    risk: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    payload_sha: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
