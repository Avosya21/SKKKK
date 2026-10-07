"""Модели ExchangeBundle и ExchangeFile — посылки файлов от внешнего агента (карты конфигурации, дипмапы сущностей)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class ExchangeBundle(Base):
    """Посылка: манифест файлов (относительный путь, размер, sha256), принимается частями, после приёма только читается.

    Статусы: open (идёт приём), complete (все файлы сошлись по sha256), deleted (удалена владельцем проекта)."""

    __tablename__ = "exchange_bundles"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_exchange_bundle_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="other")  # config_map | entity_deepmap | other
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    manifest_sha: Mapped[str] = mapped_column(String(64), nullable=False)  # хеш манифеста: повтор открытия той же посылки — докачка
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_id: Mapped[int | None] = mapped_column(ForeignKey("project_mcp_tokens.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExchangeFile(Base):
    """Файл посылки: принято received_bytes из size; status pending → complete (sha256 сошёлся)."""

    __tablename__ = "exchange_files"
    __table_args__ = (UniqueConstraint("bundle_id", "path", name="uq_exchange_file_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bundle_id: Mapped[int] = mapped_column(ForeignKey("exchange_bundles.id"), nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(400), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    received_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
