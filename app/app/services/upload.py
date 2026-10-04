"""Приём файлов: определение типа, приоритет, длительность через ffprobe, сохранение потока, хеш, поиск дубликатов и создание записи артефакта."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import subprocess
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.models.job import Job
from app.app.services.storage import (
    ensure_inside,
    generate_stored_filename,
    raw_input_dir,
)

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm", ".mp4", ".mkv"}
WORD_EXT = {".docx", ".doc", ".docm", ".dotx", ".odt"}
SHEET_EXT = {".xlsx", ".xlsm", ".xltx", ".xls", ".ods", ".csv", ".tsv"}
DOC_EXT = {".pdf", ".pptx", ".ppt", ".txt", ".md", ".markdown", ".rtf", ".log", ".json", ".yaml", ".yml", ".ini", ".text",
           ".html", ".htm"} | WORD_EXT | SHEET_EXT
TEXT_EXT = {".txt", ".md", ".markdown", ".rtf", ".log", ".json", ".yaml", ".yml", ".ini", ".text"}
WORD_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/vnd.oasis.opendocument.text",
}
SHEET_MIMES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "application/vnd.oasis.opendocument.spreadsheet",
    "text/csv",
    "text/tab-separated-values",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
TEXT_MIMES = {
    "text/plain",
    "text/markdown",
    "application/rtf",
    "application/json",
    "application/x-yaml",
}


def reconcile_source_type(source_type: str, file_type: str) -> str:
    """Тип загрузки должен соответствовать содержимому (пункт плана 3.8): документ при типе «Созвон»
    не должен считаться созвоном (и наоборот). «Заметка» — осознанный выбор, не трогаем, кроме аудио."""
    if file_type == "other":
        return source_type  # неизвестный тип: выбор пользователя не переопределяем
    if file_type == "audio":
        return "call"
    if file_type == "image":
        return "screenshot" if source_type in ("call", "document") else source_type
    if source_type in ("call", "screenshot"):
        return "document"
    return source_type


def detect_file_type(filename: str, content_type: str | None) -> str:
    ext = Path(filename).suffix.lower()
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if ext in AUDIO_EXT or mime.startswith("audio/"):
        return "audio"
    if ext in IMAGE_EXT or mime.startswith("image/"):
        return "image"
    if ext in {".html", ".htm"} or mime in {"text/html", "application/xhtml+xml"}:
        return "html"
    if ext in {".pptx", ".ppt"} or mime in {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
    }:
        return "pptx"
    # Таблицы (Excel, CSV) — до текстовых: у .csv mime text/csv, но читать его надо как таблицу.
    if ext in SHEET_EXT or (ext not in TEXT_EXT and mime in SHEET_MIMES):
        return "xlsx"
    if ext in WORD_EXT or (ext not in TEXT_EXT and mime in WORD_MIMES):
        return "docx"
    if ext in TEXT_EXT or mime in TEXT_MIMES or mime.startswith("text/"):
        return "txt"
    if ext == ".pdf" or mime == "application/pdf":
        return "pdf"
    return "other"


def default_priority(source_type: str, settings_json: dict | None = None) -> float:
    weights = (settings_json or {}).get(
        "source_weights",
        {
            "document_customer": 1.0,
            "document_internal": 0.8,
            "call": 0.5,
            "note": 0.6,
            "screenshot": 0.4,
        },
    )
    mapping = {
        "call": weights.get("call", 0.5),
        "document": weights.get("document_internal", 0.8),
        "note": weights.get("note", 0.6),
        "screenshot": weights.get("screenshot", 0.4),
    }
    return float(mapping.get(source_type, 0.5))


def ffprobe_duration(path: Path) -> float | None:
    from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool

    try:
        ffprobe = resolve_ffmpeg_tool("ffprobe")
    except FileNotFoundError:
        return None
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if out.returncode != 0:
            return None
        return float(out.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def save_upload_stream(
    dest: Path,
    stream,
    *,
    max_bytes: int,
    chunk_size: int = 1024 * 1024,
) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("file too large")
            h.update(chunk)
            f.write(chunk)
    return h.hexdigest(), size


def hash_file(path: Path, *, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return h.hexdigest(), size


def find_duplicate(db: Session, project_id: int, sha256: str) -> Artifact | None:
    return db.scalar(
        select(Artifact).where(Artifact.project_id == project_id, Artifact.sha256 == sha256)
    )


def create_artifact_record(
    db: Session,
    *,
    project_id: int,
    user_id: int,
    original_filename: str,
    stored_filename: str,
    file_path: Path,
    file_type: str,
    source_type: str,
    sha256: str,
    priority_weight: float,
    duration_sec: float | None,
    queue_job: bool = True,
    meeting_started_at: datetime | None = None,
) -> tuple[Artifact, Job | None]:
    from app.app.services.recorded_at import precision_for_source, resolve_recorded_at

    base = Path(original_filename).stem
    artifact_id = Path(stored_filename).stem
    recorded_at, recorded_source = resolve_recorded_at(
        original_filename=original_filename,
        file_path=Path(file_path) if file_path else None,
        file_type=file_type,
        meeting_started_at=meeting_started_at,
    )
    art = Artifact(
        artifact_id=artifact_id,
        project_id=project_id,
        user_id=user_id,
        original_filename=original_filename,
        stored_filename=stored_filename,
        file_path=str(file_path),
        file_type=file_type,
        source_type=source_type,
        status="uploaded",
        priority_weight=priority_weight,
        sha256=sha256,
        duration_sec=duration_sec,
        recorded_at=recorded_at,
        recorded_at_source=recorded_source,
        recorded_at_precision=precision_for_source(recorded_source),
    )
    db.add(art)
    db.flush()
    job = None
    if queue_job:  # файлы обмена с внешним агентом (analysis_reply_/analysis_request_*.json) в конвейер не ставятся
        job = Job(artifact_id=art.id, kind="process_artifact", status="queued", phase="asr")
        db.add(job)
    db.commit()
    db.refresh(art)
    if job is not None:
        db.refresh(job)
    return art, job
