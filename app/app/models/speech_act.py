"""Речевые акты реплик (docs/plans/DECISION-MEMORY-PLAN.md, Э2): что делает каждая реплика переписки или созвона в обсуждении решений.

Одна строка — один акт в одной реплике; у реплики их может быть несколько. Строки материала целиком заменяются при
повторном разборе. Цитата проверена кодом (дословно есть в реплике), модель сама номеров и текста не задаёт.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base

ACTS = ("proposed", "agreed", "rejected", "deferred", "asked", "answered", "done", "cancelled", "blocked", "info")
ACT_LABELS = {
    "proposed": "предложил", "agreed": "согласился", "rejected": "отклонил", "deferred": "отложил", "asked": "спросил",
    "answered": "ответил", "done": "сообщил о выполненном", "cancelled": "отменил прежнее", "blocked": "сообщил о препятствии", "info": "сообщил факт",
}


class SpeechAct(Base):
    __tablename__ = "speech_acts"
    __table_args__ = (Index("ix_speech_acts_artifact_unit", "artifact_id", "unit_ref"), Index("ix_speech_acts_project_act", "project_id", "act"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id"), nullable=False, index=True)
    unit_ref: Mapped[str] = mapped_column(String(24), nullable=False)  # M141 / T20
    unit_key: Mapped[int] = mapped_column(Integer, nullable=False)
    start_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    said_at: Mapped[str | None] = mapped_column(String(40), nullable=True)  # ISO; у реплики созвона — время записи + секунды, если известно
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    side: Mapped[str | None] = mapped_column(String(16), nullable=True)  # executor / customer / unknown
    act: Mapped[str] = mapped_column(String(16), nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    about: Mapped[str | None] = mapped_column(String(200), nullable=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("subjects.id"), nullable=True, index=True)  # предмет обсуждения (Э3)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
