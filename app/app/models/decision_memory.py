"""Память решений (docs/plans/DECISION-MEMORY-PLAN.md, Э3–Э5): предметы обсуждения и журнал событий по ним.

``subjects`` — то, о чём принимаются решения (одна тема обсуждения: «приложение в Битрикс24», «пометка старой номенклатуры»).
``decision_events`` — журнал, в который только дописывают: что с предметом произошло, кто это сделал и какой стороны, когда это
было сказано (``occurred_at``) и когда система узнала (``recorded_at``), в каком материале и где (адрес реплики/абзаца) и цитата.
Состояние предмета ``subjects.state`` — свёртка журнала (``services/decision_log.fold``); человек и модель-судья пишут события
того же журнала, поэтому «почему так» всегда видно по событиям.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base

# Виды событий журнала
EVENT_KINDS = ("proposed", "agreed", "rejected", "superseded", "implemented", "reopened", "withdrawn")
EVENT_LABELS = {
    "proposed": "предложено", "agreed": "согласовано", "rejected": "отклонено", "superseded": "заменено",
    "implemented": "выполнено", "reopened": "возобновлено", "withdrawn": "снято",
}
# Состояния предмета
STATES = ("open", "proposed", "agreed", "rejected", "superseded", "implemented", "withdrawn")
STATE_LABELS = {
    "open": "обсуждается", "proposed": "предложено", "agreed": "согласовано", "rejected": "отклонено", "superseded": "заменено",
    "implemented": "выполнено", "withdrawn": "снято",
}
DECIDED_BY = ("model", "rule", "human")


class Subject(Base):
    __tablename__ = "subjects"
    __table_args__ = (Index("ix_subjects_project_state", "project_id", "state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    state_since: Mapped[str | None] = mapped_column(String(40), nullable=True)  # когда наступило текущее состояние (occurred_at решающего события)
    state_source: Mapped[str | None] = mapped_column(String(16), nullable=True)  # fold | judge | human
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # итог словами (от судьи Э5), пока пусто
    basis_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # номера актов, на которые опёрся судья (Э5)
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey("subjects.id"), nullable=True)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)  # центр по актам предмета (Э3); пусто — не считался
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DecisionEvent(Base):
    __tablename__ = "decision_events"
    __table_args__ = (Index("ix_decision_events_subject_time", "subject_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    actor_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    actor_side: Mapped[str | None] = mapped_column(String(16), nullable=True)  # сторона на момент события
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)  # ISO, сортируется как текст; нет времени — день (ГГГГ-ММ-ДД)
    precision: Mapped[str] = mapped_column(String(8), nullable=False, default="minute")  # minute | day
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    artifact_id: Mapped[int | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True, index=True)
    anchor: Mapped[str | None] = mapped_column(String(24), nullable=True)  # M141 / T20 / P12
    quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    speech_act_id: Mapped[int | None] = mapped_column(ForeignKey("speech_acts.id"), nullable=True)
    supersedes_event_id: Mapped[int | None] = mapped_column(ForeignKey("decision_events.id"), nullable=True)
    decided_by: Mapped[str] = mapped_column(String(8), nullable=False, default="rule")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
