from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.app.db import Base


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    upload_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(String(32), default="uploaded", nullable=False)
    priority_weight: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    transcription_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcription_file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    telegram_notification_sent: Mapped[bool] = mapped_column(default=False, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    asr_engines_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_html_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    project: Mapped["Project"] = relationship(back_populates="artifacts")
    jobs: Mapped[list["Job"]] = relationship(back_populates="artifact")
    entities: Mapped[list["Entity"]] = relationship(back_populates="artifact")
