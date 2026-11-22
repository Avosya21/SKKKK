"""Снятые с личной доски задачи (таблица user_board_dismissals): человек убрал строку, работа ещё не сделана."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base

KIND_SPEAKERS = "speakers"
KIND_MEETING = "meeting"


class UserBoardDismissal(Base):
    __tablename__ = "user_board_dismissals"
    __table_args__ = (UniqueConstraint("user_id", "kind", "ref", name="uq_user_board_dismissal"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    ref: Mapped[str] = mapped_column(String(64), nullable=False)
