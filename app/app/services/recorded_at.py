"""Когда записан материал (`artifacts.recorded_at`): встреча из комнаты, дата в имени файла, метаданные файла или дата загрузки."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SOURCE_MEETING = "meeting"
SOURCE_FILENAME = "filename"
SOURCE_MEDIA = "media"
SOURCE_UPLOAD = "upload"
# Источники, которым можно верить как дате созвона; «upload» — только замена.
EXPLICIT_SOURCES = frozenset({SOURCE_MEETING, SOURCE_FILENAME, SOURCE_MEDIA})

# Пояс показа по умолчанию — как у контракта (московское время); в БД всё хранится в UTC.
LOCAL_TZ = timezone(timedelta(hours=3))

_FILENAME_DATE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](20\d{2}))?(?!\d)")
_MEDIA_EXT = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm", ".mp4", ".mkv", ".mov"}


def filename_date(name: str | None, upload_date: datetime | None = None) -> date | None:
    """Дата вида 19.08 / 19.08.2026 из имени файла. Без года — год загрузки (или прошлый, если месяц «из будущего»)."""
    match = _FILENAME_DATE.search(name or "")
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    year = int(match.group(3)) if match.group(3) else None
    if year is None:
        if upload_date:
            year = upload_date.year
            if month > upload_date.month + 2:
                year -= 1
        else:
            year = datetime.now().year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _as_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def media_creation_time(path: Path) -> datetime | None:
    """``format.tags.creation_time`` из ffprobe (UTC). Запись из браузера (.webm) его обычно не несёт — тогда None."""
    from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool

    if path.suffix.lower() not in _MEDIA_EXT or not path.is_file():
        return None
    try:
        ffprobe = resolve_ffmpeg_tool("ffprobe")
    except FileNotFoundError:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format_tags=creation_time", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        tags = (json.loads(out.stdout or "{}").get("format") or {}).get("tags") or {}
    except ValueError:
        return None
    raw = str(tags.get("creation_time") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # Нулевые/заводские даты контейнера (1970, 1904) — не дата созвона.
    if parsed.year < 2000:
        return None
    return _as_utc_naive(parsed)


def resolve_recorded_at(
    *,
    original_filename: str,
    file_path: Path | None,
    file_type: str,
    meeting_started_at: datetime | None = None,
    upload_date: datetime | None = None,
) -> tuple[datetime, str]:
    """Дата записи и её источник. Порядок: встреча → имя файла → метаданные → дата загрузки."""
    if meeting_started_at is not None:
        return _as_utc_naive(meeting_started_at), SOURCE_MEETING
    uploaded = upload_date or datetime.now(timezone.utc).replace(tzinfo=None)
    by_name = filename_date(original_filename, uploaded)
    if by_name is not None:
        # Точность «день»: полдень UTC, чтобы день не съезжал ни в одном поясе ±11 ч.
        return datetime(by_name.year, by_name.month, by_name.day, 12, 0), SOURCE_FILENAME
    if file_type == "audio" and file_path is not None:
        media = media_creation_time(file_path)
        if media is not None:
            return media, SOURCE_MEDIA
    return _as_utc_naive(uploaded), SOURCE_UPLOAD


def to_local(dt: datetime | None, tz: timezone = LOCAL_TZ) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def recorded_local_date(artifact: Any) -> date | None:
    """Местная дата записи, если источник явный (не «дата загрузки»)."""
    if getattr(artifact, "recorded_at_source", None) not in EXPLICIT_SOURCES:
        return None
    local = to_local(getattr(artifact, "recorded_at", None))
    return local.date() if local else None


def material_date_label(artifact: Any) -> str:
    """Подпись даты для LLM: «созвон 21.09.2026» или честное «дата загрузки 21.09.2026»."""
    explicit = recorded_local_date(artifact)
    if explicit is not None:
        kind = "созвон" if getattr(artifact, "file_type", "") == "audio" else "дата материала"
        return f"{kind} {explicit:%d.%m.%Y}"
    by_name = filename_date(getattr(artifact, "original_filename", None), getattr(artifact, "upload_date", None))
    if by_name is not None:
        kind = "созвон" if getattr(artifact, "file_type", "") == "audio" else "дата материала"
        return f"{kind} {by_name:%d.%m.%Y}"
    uploaded = to_local(getattr(artifact, "upload_date", None))
    if uploaded is None:
        return ""
    return f"дата загрузки {uploaded:%d.%m.%Y} (дата созвона неизвестна)"
