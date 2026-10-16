"""Реестр людей проекта (docs/plans/PLATFORM-FOUNDATION-PLAN.md, ПФ1).

Человек — сущность проекта: у сотрудника есть ``user_id``, у заказчика без учётки его нет. Сторона (исполнитель /
заказчик) — свойство участия в проекте. Все написания имени хранятся в ``person_aliases``; неопознанные авторы,
спикеры и гости ждут ответа в ``unknown_actors``. Каждая правка реестра пишется в ``people_log``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base

SIDE_EXECUTOR = "executor"
SIDE_CUSTOMER = "customer"
SIDE_UNKNOWN = "unknown"
SIDES = (SIDE_EXECUTOR, SIDE_CUSTOMER, SIDE_UNKNOWN)

SIDE_SOURCES = ("card", "project_party", "human", "model")
AUTHORITIES = ("approver", "participant", "observer")

ALIAS_SOURCES = ("card", "chat", "voice", "room", "document", "human", "model")

ACTOR_KINDS = ("chat_author", "speaker_label", "guest", "document_author")
ACTOR_STATUSES = ("open", "resolved", "ignored")


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    # Учётка портала — только у сотрудников; заказчик без входа остаётся человеком проекта с пустым user_id.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False, default=SIDE_UNKNOWN)
    side_source: Mapped[str] = mapped_column(String(16), nullable=False, default="model")
    job_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # approver — может утверждать решения; пока не задано (NULL), утверждающим считается любой сотрудник заказчика (Д3).
    authority: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Внешний человек: слепок голоса создаётся только после отметки менеджера «согласие получено» (Д5).
    voice_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voice_consent_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    merged_into: Mapped[int | None] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Компания проставлена автоматически (гость встречи без указанной компании — заказчик проекта, ЛГ5): ждёт подтверждения
    # менеджера в «Люди и стороны»; снимается кнопкой «Компания верна» или правкой компании.
    company_unconfirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # Карточку завела система (автор переписки, гость встречи): в «Люди и стороны» она подсвечена, пока менеджер её не
    # подтвердит («Подтвердить карточку») или не объединит с уже известным человеком.
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PersonAlias(Base):
    """Написание имени, которое указывает на человека: ключ (без регистра, ё=е, пробелы) уникален в проекте."""

    __tablename__ = "person_aliases"
    __table_args__ = (UniqueConstraint("project_id", "alias_key", name="uq_person_aliases_project_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(200), nullable=False)
    alias_key: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="human")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectParty(Base):
    """Сторона компании в проекте (Д1): значение по умолчанию — от компании, здесь — для этого проекта."""

    __tablename__ = "project_parties"
    __table_args__ = (UniqueConstraint("project_id", "company_key", name="uq_project_parties_project_company"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    company_key: Mapped[str] = mapped_column(String(200), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UnknownActor(Base):
    """Очередь «Кто это?»: автор чата, метка спикера, гость комнаты или автор документа без человека в реестре."""

    __tablename__ = "unknown_actors"
    __table_args__ = (UniqueConstraint("project_id", "artifact_id", "kind", "label_key", name="uq_unknown_actors_label"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    # Метки speaker_N живут в одном материале, поэтому ключ включает материал; имя автора чата опознаётся по алиасу для всех.
    artifact_id: Mapped[int | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    label_key: Mapped[str] = mapped_column(String(200), nullable=False)
    examples_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    suggestions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PeopleLog(Base):
    """Журнал правок реестра (Д6): кто, когда, что и как было до и после."""

    __tablename__ = "people_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
