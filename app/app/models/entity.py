"""Модели знаний: сущности, связи, фрагменты, запуски ASR, заметки и ссылки, разрешения конфликтов, журнал изменений ТЗ."""

from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.app.db import Base


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id"), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Тема проекта (REQUIREMENTS-PLAN этап 5); без внешнего ключа — тема может пересобираться независимо от сущностей.
    topic_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    artifact: Mapped["Artifact"] = relationship(back_populates="entities")


class Topic(Base):
    """Тема проекта: потребность или объект («ввод характеристик номенклатуры»), а не способ решения (решение А2).
    По теме модель склеивает дубли и находит устаревшее (REQUIREMENTS-PLAN этапы 5–7)."""

    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Когда тему последний раз проверяли на дубли и устаревшее; сбрасывается, когда в тему приходят новые пункты.
    reviewed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Link(Base):
    __tablename__ = "links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    source_entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    target_entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    link_type: Mapped[str] = mapped_column(String(64), nullable=False)
    similarity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_automatic: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Вердикт локальной модели по конфликту (JSON: verdict, reason, confidence, mode, prompt_version…); человек главнее.
    model_review: Mapped[str | None] = mapped_column(Text, nullable=True)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id"), nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(128), nullable=True)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Отрезок созвона, который покрывает фрагмент (секунды от начала записи); у документов и старых чанков — NULL.
    start_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Адреса единиц источника, которые покрывает фрагмент: переписка — сообщения «M101»…«M108» (ссылка открывает сообщение).
    unit_first: Mapped[str | None] = mapped_column(String(24), nullable=True)
    unit_last: Mapped[str | None] = mapped_column(String(24), nullable=True)


class AsrRun(Base):
    __tablename__ = "asr_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id"), nullable=False, index=True)
    engine: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class UserNote(Base):
    __tablename__ = "user_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class NoteRef(Base):
    __tablename__ = "note_refs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("user_notes.id"), nullable=False, index=True)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)


class Resolution(Base):
    __tablename__ = "resolutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("links.id"), nullable=False, unique=True)
    winning_entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False)
    losing_entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="resolved", nullable=False)


class Changelog(Base):
    __tablename__ = "changelog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    version_label: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_ids_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    summary_of_changes: Mapped[str] = mapped_column(Text, nullable=False)
    tz_path: Mapped[str] = mapped_column(String(1024), nullable=False)
