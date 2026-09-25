"""Модель ContractItem — элемент контракта обмена с внешним агентом."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class ContractItem(Base):
    """Реестр стабильных идентификаторов контракта обмена (req-/dec-/cft-…): id не меняется при переизвлечении сущностей.

    key — нормализованный текст утверждения; при небольшой правке формулировки id сохраняется по близости текста.
    content_hash нужен дельта-каналу get_changes: что добавилось, изменилось, исчезло."""

    __tablename__ = "contract_items"
    __table_args__ = (UniqueConstraint("project_id", "kind", "key", name="ux_contract_items_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # req | dec | cft
    key: Mapped[str] = mapped_column(String(512), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
