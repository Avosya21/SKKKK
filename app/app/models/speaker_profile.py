from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class SpeakerProfile(Base):
    """Отпечаток голоса: global — общая база подтверждённых; project — локальная копия."""

    __tablename__ = "speaker_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), default="project", index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(256))
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    embedding_dim: Mapped[int] = mapped_column(Integer, default=256)
    source_artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_start_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_end_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ArtifactSpeakerLink(Base):
    """Привязка SPEAKER_XX артефакта к профилю (подсказка или подтверждение)."""

    __tablename__ = "artifact_speaker_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"), index=True)
    diar_label: Mapped[str] = mapped_column(String(64))
    profile_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("speaker_profiles.id"), nullable=True)
    suggested_profile_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("speaker_profiles.id"), nullable=True)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="suggested")
    clip_start_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    clip_end_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
