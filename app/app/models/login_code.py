"""Одноразовые коды входа (Битрикс24 / email)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class LoginCode(Base):
    __tablename__ = "login_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    login_key: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)  # b24 | email
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    b24_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class B24Employee(Base):
    """Кэш email → id сотрудника в Битрикс24 (как employees в iv-cowork)."""

    __tablename__ = "b24_employees"

    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    b24_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
