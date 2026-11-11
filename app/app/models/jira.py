"""Модели подключения Jira (docs/plans/JIRA-INTEGRATION-PLAN.md, этап 2).

``jira_accounts`` — личный токен Jira пользователя портала (зашифрован), ``jira_project_links`` — привязка проекта портала
к проекту Jira (ключ Jira привязан не более чем к одному проекту), ``jira_items`` — что уже скачано (задача или вложение →
артефакт портала), ``jira_grants`` — доступ к проекту, выданный потому, что токен пользователя видит проект в Jira.
Ссылки на пользователей, проекты и артефакты — без внешних ключей: строки убирает ``jira_import.forget_project`` при удалении
проекта, а удалённый вручную материал остаётся «скачанным» и заново не загружается.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class JiraAccount(Base):
    __tablename__ = "jira_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    token_enc: Mapped[str] = mapped_column(Text, nullable=False)  # токен, зашифрованный ключом из SECRET_KEY
    jira_name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # чей токен, по ответу Jira
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ключи проектов Jira, которые токен видел при последней проверке (через запятую) — чтобы выдать доступ сразу при новой привязке
    visible_keys: Mapped[str | None] = mapped_column(Text, nullable=True)


class JiraProjectLink(Base):
    __tablename__ = "jira_project_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    jira_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    jira_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # idle | queued | running | error — ход загрузки из Jira
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="idle")
    status_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JiraItem(Base):
    __tablename__ = "jira_items"
    __table_args__ = (UniqueConstraint("link_id", "kind", "ref", name="uq_jira_items_ref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    link_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # issue | attachment
    ref: Mapped[str] = mapped_column(String(128), nullable=False)  # ключ задачи или id вложения
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    updated: Mapped[str | None] = mapped_column(String(40), nullable=True)  # отметка «обновлено» из Jira
    artifact_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)  # почему пропущено (вид файла, размер)


class JiraGrant(Base):
    __tablename__ = "jira_grants"
    __table_args__ = (UniqueConstraint("user_id", "project_id", name="uq_jira_grants"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
