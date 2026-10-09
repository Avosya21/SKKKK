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
EVENT_KINDS = ("proposed", "agreed", "rejected", "superseded", "implemented", "reopened", "withdrawn", "documented")
EVENT_LABELS = {
    "proposed": "предложено", "agreed": "согласовано", "rejected": "отклонено", "superseded": "заменено",
    "implemented": "выполнено", "reopened": "возобновлено", "withdrawn": "снято", "documented": "зафиксировано в документе",
}
# Состояния предмета
# documented — «в документе»: записано в документе, но ни созвон, ни переписка, ни признак утверждения его не подтвердили
STATES = ("open", "proposed", "documented", "agreed", "rejected", "superseded", "implemented", "withdrawn")
STATE_LABELS = {
    "open": "обсуждается", "proposed": "предложено", "documented": "в документе", "agreed": "согласовано", "rejected": "отклонено",
    "superseded": "заменено", "implemented": "выполнено", "withdrawn": "снято",
}
DECIDED_BY = ("model", "rule", "human")
# Состояния позиции внутри предмета (вариант или значение): действует / отклонена / заменена / открыта
POSITION_STATES = ("current", "rejected", "superseded", "open")
POSITION_LABELS = {"current": "действует", "rejected": "отклонено", "superseded": "заменено", "open": "обсуждается"}


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
    judged_sig: Mapped[str | None] = mapped_column(String(64), nullable=True)  # «число актов:наибольший номер» на момент итога судьи (инкремент Э8)
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


class SubjectPosition(Base):
    """Позиция внутри предмета: вариант решения или значение («пометка “!”», «цена 126 000»), у которой своё состояние.
    Предмет «цена работ» целиком согласован, но позиция «144 000» в нём заменена позицией «126 000» — это пишет судья (Э5)."""
    __tablename__ = "subject_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    basis_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # номера актов-оснований
    replaced_by: Mapped[str | None] = mapped_column(String(160), nullable=True)  # название позиции, которая заменила эту
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryShadowReview(Base):
    """Расхождение «память решений против прежнего контура» по пункту проекта и вердикт арбитра (модель; человек может переоценить).
    Строится ``services/memory_compare``; по доле «права память» решается готовность проекта к режиму live."""
    __tablename__ = "memory_shadow_review"
    __table_args__ = (Index("ix_memory_shadow_project", "project_id", "entity_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)  # пункт прежнего контура (без внешнего ключа: пункты пересобираются)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("subjects.id"), nullable=True)
    old_state: Mapped[str] = mapped_column(String(16), nullable=False)  # current | done | superseded | duplicate | rejected
    new_state: Mapped[str] = mapped_column(String(16), nullable=False)  # то же, по памяти решений
    verdict: Mapped[str] = mapped_column(String(10), nullable=False)  # old | new | neither | unclear
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    by: Mapped[str] = mapped_column(String(10), nullable=False, default="model")  # model | human
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
