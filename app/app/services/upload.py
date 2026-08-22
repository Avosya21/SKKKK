from __future__ import annotations

import hashlib
import json
import mimetypes
import subprocess
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
DOC_EXT = {".pdf", ".docx", ".doc", ".txt", ".md", ".markdown", ".rtf", ".csv", ".log", ".json", ".yaml", ".yml", ".ini", ".text", ".html", ".htm"}
TEXT_EXT = {".txt", ".md", ".markdown", ".rtf", ".csv", ".log", ".json", ".yaml", ".yml", ".ini", ".text"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
TEXT_MIMES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/tab-separated-values",
    "application/rtf",
    "application/json",
    "application/x-yaml",
}


def detect_file_type(filename: str, content_type: str | None) -> str:
    ext = Path(filename).suffix.lower()
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if ext in AUDIO_EXT or mime.startswith("audio/"):
        return "audio"
    if ext in IMAGE_EXT or mime.startswith("image/"):
        return "image"
    if ext in {".html", ".htm"} or mime in {"text/html", "application/xhtml+xml"}:
        return "html"
    if ext in TEXT_EXT or mime in TEXT_MIMES or mime.startswith("text/"):
        return "txt"
    if ext in DOC_EXT or mime in (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ):
        if ext == ".docx":
            return "docx"
        if ext == ".pdf":
            return "pdf"
        return "other"
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
) -> tuple[Artifact, Job]:
    base = Path(original_filename).stem
    artifact_id = Path(stored_filename).stem
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
    )
    db.add(art)
    db.flush()
    job = Job(artifact_id=art.id, kind="process_artifact", status="queued", phase="asr")
    db.add(job)
    db.commit()
    db.refresh(art)
    db.refresh(job)
    return art, job
