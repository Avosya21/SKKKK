"""Модели ключа проектного MCP: ProjectMcpToken (хеш, права, отзыв) и McpCallStat (сколько и чего ключ читал по дням)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class ProjectMcpToken(Base):
    __tablename__ = "project_mcp_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # право класть вопросы/вердикты в очередь обмена (submit_*): по умолчанию выключено, задаёт владелец проекта при выдаче ключа
    can_submit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # права ключа: read | questions | questions_verdicts (can_submit хранится согласованно: scope != read)
    scope: Mapped[str] = mapped_column(String(24), nullable=False, default="read", server_default="read")
    agent_name: Mapped[str | None] = mapped_column(String(64), nullable=True)  # кто этим ключом пользуется; показывается в журнале
    rate_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # обращений за 10 минут; пусто — общий лимит
    # право на посылки файлов (services/exchange_files.py): blocked | read | write; не зависит от scope.
    # По умолчанию read: принятые посылки — данные проекта; прежнее значение none в старых строках тоже читается как read.
    files_access: Mapped[str] = mapped_column(String(8), nullable=False, default="read", server_default="read")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class McpCallStat(Base):
    """Счётчик вызовов инструментов MCP: ключ × день × инструмент (MCP-KB-PLAN Э4). Содержимого ответов здесь нет — только
    сколько раз, сколько байт и сколько ошибок; подробности каждого вызова — строка `mcp.call` в api.log."""

    __tablename__ = "mcp_call_stats"
    __table_args__ = (UniqueConstraint("token_id", "day", "tool", name="uq_mcp_call_stat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("project_mcp_tokens.id"), nullable=False, index=True)
    day: Mapped[str] = mapped_column(String(10), nullable=False)  # ГГГГ-ММ-ДД по UTC
    tool: Mapped[str] = mapped_column(String(48), nullable=False)
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
