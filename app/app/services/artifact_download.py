"""Скачивание исходного файла артефакта: путь, имя и MIME (resolve_artifact_download), доступность и подпись кнопки «Скачать»."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.services.artifact_audio import ensure_audio_download_mp3
from app.app.services.storage import (
    artifact_audio_download_mp3_path,
    artifact_audio_path,
    ensure_inside,
    project_root,
)


def _audio_download_name(artifact: Artifact) -> str:
    stem = Path(artifact.original_filename).stem or artifact.artifact_id
    return f"{stem}.mp3"


def resolve_artifact_download(
    settings: Settings,
    artifact: Artifact,
) -> tuple[Path, str, str] | None:
    """Путь, MIME и имя файла для скачивания (None — ещё недоступно)."""
    if artifact.file_type == "audio":
        mp3 = ensure_audio_download_mp3(settings, artifact)
        if not mp3:
            return None
        return mp3, "audio/mpeg", _audio_download_name(artifact)

    src = Path(artifact.file_path)
    if not src.is_file():
        return None
    try:
        ensure_inside(project_root(settings, artifact.project_id), src)
    except ValueError:
        return None
    media_type = mimetypes.guess_type(artifact.original_filename)[0] or "application/octet-stream"
    return src, media_type, artifact.original_filename


def artifact_download_available(settings: Settings, artifact: Artifact) -> bool:
    """Только проверка файлов. Извлечение WAV здесь нельзя: опрос списка
    артефактов иначе гоняет ffmpeg параллельно с GPU-воркером."""
    if artifact.file_type == "audio":
        mp3 = artifact_audio_download_mp3_path(settings, artifact.artifact_id, artifact.project_id)
        if mp3.is_file() and mp3.stat().st_size >= 1000:
            return True
        wav = artifact_audio_path(settings, artifact.artifact_id, artifact.project_id)
        return wav.is_file() and wav.stat().st_size >= 1000
    src = Path(artifact.file_path)
    if not src.is_file():
        return False
    try:
        ensure_inside(project_root(settings, artifact.project_id), src)
    except ValueError:
        return False
    return True


def artifact_download_label(artifact: Artifact) -> str | None:
    if artifact.file_type == "audio":
        return _audio_download_name(artifact)
    return artifact.original_filename or None
