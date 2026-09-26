"""Модель Company — компания из файла Companies.txt: название и сторона (исполнитель / заказчик)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base

SIDE_EXECUTOR = "executor"
SIDE_CUSTOMER = "customer"


class Company(Base):
    """Источник истины — файл `Companies.txt`; таблица — его отражение для ссылок из карточек."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Без регистра — уникальность и поиск: «Acme» и «ACME» — одна компания.
    name_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    # executor | customer | "" (не указана). Пользователю не показывается — нужна анализу.
    side: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # Убранная из файла компания не удаляется (у людей она остаётся), а скрывается из списка.
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
