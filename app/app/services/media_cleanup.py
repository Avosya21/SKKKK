"""Чистка исходников: удаление видео после проверки извлечённого WAV и сверка пути исходника артефакта."""

from __future__ import annotations

import logging
from pathlib import Path

from app.app.logging_setup import log_event
from app.app.services.upload import ffprobe_duration

logger = logging.getLogger("skkkk.media_cleanup")

# Контейнеры с видеодорожкой: после извлечения WAV оригинал не нужен.
VIDEO_CONTAINER_EXT = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".mpeg",
    ".mpg",
    ".wmv",
    ".flv",
    ".m4v",
}

# ~0.25 с PCM 16 kHz mono s16le
_MIN_WAV_BYTES = 8_000


def is_video_container(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_CONTAINER_EXT


def verify_extracted_wav(wav: Path, *, min_duration_sec: float = 0.5) -> tuple[bool, str]:
    """Проверить, что из видео/аудио успешно извлечён рабочий WAV."""
    if not wav.is_file():
        return False, "wav missing"
    size = wav.stat().st_size
    if size < _MIN_WAV_BYTES:
        return False, f"wav too small ({size} B)"
    duration = ffprobe_duration(wav)
    if duration is None:
        return False, "ffprobe failed"
    if duration < min_duration_sec:
        return False, f"duration too short ({duration:.2f}s)"
    return True, f"{duration:.1f}s, {size} B"


def maybe_discard_video_source(source: Path, wav_16k: Path, *, artifact_id: str = "") -> bool:
    """Удалить видео-оригинал из raw_inputs после проверенного извлечения звука."""
    if not is_video_container(source):
        return False
    if not source.is_file():
        return False

    ok, detail = verify_extracted_wav(wav_16k)
    if not ok:
        log_event(
            logger,
            "media.discard.skip",
            artifact=artifact_id or None,
            source=source.name,
            reason=detail,
        )
        return False

    try:
        source.unlink()
    except OSError as exc:
        log_event(
            logger,
            "media.discard.failed",
            artifact=artifact_id or None,
            source=str(source),
            error=str(exc),
        )
        return False

    log_event(
        logger,
        "media.discard.ok",
        artifact=artifact_id or None,
        source=source.name,
        wav=wav_16k.name,
        detail=detail,
    )
    return True


def reconcile_artifact_source_path(artifact_file_path: str, wav_path: Path) -> str | None:
    """Если file_path указывал на удалённое видео — переключить на WAV в artifacts/."""
    src = Path(artifact_file_path)
    if not is_video_container(src):
        return None
    if src.is_file():
        return None
    if not wav_path.is_file():
        return None
    ok, _ = verify_extracted_wav(wav_path)
    if not ok:
        return None
    return str(wav_path)
