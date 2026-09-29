"""Когда записан материал (`artifacts.recorded_at`): встреча из комнаты, имя файла (с временем или без), метаданные записи, шапка и свойства документа, переписка, ручная дата или дата загрузки."""

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
SOURCE_MANUAL = "manual"  # дату поставил человек в карточке материала (REQUIREMENTS-PLAN этап 1)
SOURCE_MANUAL_TIME = "manual_time"  # человек поставил дату и время начала (SOURCE-ANCHORS-PLAN Я5): точность — минута
SOURCE_MESSAGES = "messages"  # переписка: первое сообщение выгрузки; у каждого пункта — время своего сообщения
SOURCE_FILENAME_TIME = "filename_time"  # в имени файла дата и время («2026-08-26 14-30»): точность — минута
SOURCE_DOC_TEXT = "doc_text"  # дата в шапке документа («Протокол от 19.08.2026»)
SOURCE_DOC_META = "doc_meta"  # свойства файла: docx/xlsx/pptx core.xml, PDF, EXIF снимка — дата последней правки
# Источники, которым можно верить как дате созвона; «upload» — только замена.
EXPLICIT_SOURCES = frozenset({SOURCE_MEETING, SOURCE_FILENAME, SOURCE_FILENAME_TIME, SOURCE_MEDIA, SOURCE_MANUAL,
                              SOURCE_MANUAL_TIME, SOURCE_MESSAGES, SOURCE_DOC_TEXT, SOURCE_DOC_META})
# Известно время начала записи с точностью до минуты: таймкод реплики даёт момент (Я3).
MINUTE_SOURCES = frozenset({SOURCE_MEETING, SOURCE_MEDIA, SOURCE_MANUAL_TIME, SOURCE_FILENAME_TIME})
# Дату, выведенную автоматически из этих источников, можно уточнить автоматически (текст документа точнее свойств файла).
AUTO_REFINABLE = frozenset({SOURCE_UPLOAD, SOURCE_DOC_META, None})
DOC_META_TYPES = frozenset({"docx", "xlsx", "pptx", "pdf", "image"})

# Пояс показа по умолчанию — как у контракта (московское время); в БД всё хранится в UTC.
LOCAL_TZ = timezone(timedelta(hours=3))

# 2026-07-22, 2026_07_22
_FILENAME_ISO = re.compile(r"(?<!\d)(20\d{2})([-_.])(\d{1,2})\2(\d{1,2})(?!\d)")
# 22.07.2026, 22_07_26, 22-07-2026: с «_» и «-» год обязателен — иначе «v1_2» стало бы датой.
_FILENAME_DMY = re.compile(r"(?<!\d)(\d{1,2})([-_./])(\d{1,2})\2(20\d{2}|\d{2})(?![\d])")
# 19.08, 5.09 без года; «8.3» (версия платформы 1С) — не дата: день или месяц должны быть двузначными.
_FILENAME_DM = re.compile(r"(?<![\d.])(\d{1,2})[./](\d{1,2})(?![\d]|\.\d)")
_MEDIA_EXT = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm", ".mp4", ".mkv", ".mov"}


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def filename_date(name: str | None, upload_date: datetime | None = None) -> date | None:
    """Дата из имени файла: 2026-07-22, 22.07.2026, 22_07_26, 22-07-2026, 19.08 / 5.09.
    Без года — год загрузки (или прошлый, если месяц «из будущего»). «8.3» — версия, не дата."""
    text = name or ""
    for match in _FILENAME_ISO.finditer(text):
        found = _safe_date(int(match.group(1)), int(match.group(3)), int(match.group(4)))
        if found:
            return found
    for match in _FILENAME_DMY.finditer(text):
        raw_year = match.group(4)
        if len(raw_year) == 2 and (len(match.group(1)) == 1 or len(match.group(3)) == 1):
            continue  # «8.3.18» — версия платформы, не 08.03.2018
        year = int(raw_year) + 2000 if len(raw_year) == 2 else int(raw_year)
        found = _safe_date(year, int(match.group(3)), int(match.group(1)))
        if found:
            return found
    for match in _FILENAME_DM.finditer(text):
        day_raw, month_raw = match.group(1), match.group(2)
        if len(day_raw) == 1 and len(month_raw) == 1:
            continue
        day, month = int(day_raw), int(month_raw)
        if upload_date:
            year = upload_date.year
            if month > upload_date.month + 2:
                year -= 1
        else:
            year = datetime.now().year
        found = _safe_date(year, month, day)
        if found:
            return found
    for match in _FILENAME_WORD.finditer(text):
        day, month = int(match.group(1)), RU_MONTHS_GEN.index(match.group(2).lower()) + 1
        if match.group(3):
            year = int(match.group(3))
        else:
            base = upload_date or datetime.now()
            year = base.year - (month > base.month + 2)  # как у «19.08» без года: месяц «из будущего» — прошлый год
        found = _safe_date(year, month, day)
        if found:
            return found
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


# «2026-08-26 14-30», «2026-08-26_14.30.12», «26.08.2026 14:30», «22.09.26 14-00-34» (Телемост) — дата и время начала записи
_FILENAME_ISO_TIME = re.compile(r"(?<!\d)(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})[ _T-]+(\d{1,2})[-_.:h](\d{2})(?!\d)")
_FILENAME_DMY_TIME = re.compile(r"(?<!\d)(\d{1,2})[._-](\d{1,2})[._-](20\d{2}|\d{2})[ _T-]+(\d{1,2})[-_.:h](\d{2})(?!\d)")
RU_MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
# «14 мая», «[14 мая 2026» в имени файла (выгрузка сообщения, протокол)
_FILENAME_WORD = re.compile(r"(?<!\d)(\d{1,2})\s+(" + "|".join(RU_MONTHS_GEN) + r")(?:\s+(20\d{2}))?", re.IGNORECASE)
# Дата в шапке документа: «от 19.08.2026», «Дата: 19.08.2026», «от 19 августа 2026 г.»
_TEXT_DATE_NUM = re.compile(r"(?:\bот|\bдата(?:\s+составления|\s+документа)?\s*:?)\s*(\d{1,2})[./](\d{1,2})[./](20\d{2})\b", re.IGNORECASE)
_TEXT_DATE_WORD = re.compile(r"\bот\s+[«\"]?(\d{1,2})[»\"]?\s+(" + "|".join(RU_MONTHS_GEN) + r")\s+(20\d{2})\b", re.IGNORECASE)
TEXT_HEADER_CHARS = 1500


def filename_datetime(name: str | None) -> datetime | None:
    """Дата и время начала из имени файла (местное время) → UTC без пояса; только дата — None (это `filename_date`)."""
    text = name or ""
    for pattern, order in ((_FILENAME_ISO_TIME, (1, 2, 3)), (_FILENAME_DMY_TIME, (3, 2, 1))):
        for m in pattern.finditer(text):
            year, month, day = (int(m.group(i)) for i in order)
            if year < 100:
                if len(m.group(1)) == 1 or len(m.group(2)) == 1:
                    continue  # «8.3.18 10-00» — версия платформы, не дата (как в filename_date)
                year += 2000
            hh, mm = int(m.group(4)), int(m.group(5))
            if hh > 23 or mm > 59 or _safe_date(year, month, day) is None:
                continue
            return _as_utc_naive(datetime(year, month, day, hh, mm, tzinfo=LOCAL_TZ))
    return None


def _sane(moment: datetime | None) -> datetime | None:
    """Отбросить заводские и будущие даты свойств файла (1980, 1601, «завтра»)."""
    if moment is None:
        return None
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    if aware.year < 2000 or aware > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return aware


def document_meta_date(path: Path | None, file_type: str) -> datetime | None:
    """Дата последней правки из свойств файла (UTC без пояса): docx/xlsx/pptx — docProps/core.xml (modified, иначе created),
    PDF — ModDate/CreationDate, снимок — EXIF DateTimeOriginal. Нет или битые — None."""
    if path is None or not Path(path).is_file() or file_type not in DOC_META_TYPES:
        return None
    try:
        if file_type in ("docx", "xlsx", "pptx"):
            import zipfile

            with zipfile.ZipFile(path) as zf:
                core = zf.read("docProps/core.xml").decode("utf-8", errors="replace")
            for tag in ("dcterms:modified", "dcterms:created"):
                m = re.search(rf"<{tag}[^>]*>([^<]+)</{tag}>", core)
                if m:
                    found = _sane(datetime.fromisoformat(m.group(1).strip().replace("Z", "+00:00")))
                    if found:
                        return _as_utc_naive(found)
        elif file_type == "pdf":
            import pymupdf

            with pymupdf.open(path) as doc:
                meta = doc.metadata or {}
            for key in ("modDate", "creationDate"):
                m = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?(?:([+-Z])(\d{2})?'?(\d{2})?)?", str(meta.get(key) or ""))
                if not m:
                    continue
                tz = timezone.utc
                if m.group(7) in ("+", "-") and m.group(8):
                    delta = timedelta(hours=int(m.group(8)), minutes=int(m.group(9) or 0))
                    tz = timezone(delta if m.group(7) == "+" else -delta)
                parts = [int(g) if g else 0 for g in m.groups()[:6]]
                found = _sane(datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], tzinfo=tz))
                if found:
                    return _as_utc_naive(found)
        elif file_type == "image":
            from PIL import Image

            with Image.open(path) as img:
                exif = img.getexif()
                raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)
            if raw:
                found = _sane(datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S").replace(tzinfo=LOCAL_TZ))
                if found:
                    return _as_utc_naive(found)
    except Exception:  # noqa: BLE001 — свойства файла необязательны: битый архив/PDF не мешает загрузке
        return None
    return None


def text_header_date(text: str, upload_date: datetime | None = None) -> date | None:
    """Дата, которую документ называет сам в шапке («Протокол от 19.08.2026», «Дата: 19.08.2026», «от 19 августа 2026»).
    Только первые 1500 знаков: дальше — сроки и даты событий, а не дата документа. Дата позже загрузки — не она."""
    head = (text or "")[:TEXT_HEADER_CHARS]
    candidates: list[tuple[int, date]] = []
    for m in _TEXT_DATE_NUM.finditer(head):
        found = _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if found:
            candidates.append((m.start(), found))
    for m in _TEXT_DATE_WORD.finditer(head):
        found = _safe_date(int(m.group(3)), RU_MONTHS_GEN.index(m.group(2).lower()) + 1, int(m.group(1)))
        if found:
            candidates.append((m.start(), found))
    limit = (upload_date or datetime.now()).date() + timedelta(days=1)
    for _pos, found in sorted(candidates):
        if found <= limit:
            return found
    return None


def resolve_recorded_at(
    *,
    original_filename: str,
    file_path: Path | None,
    file_type: str,
    meeting_started_at: datetime | None = None,
    upload_date: datetime | None = None,
) -> tuple[datetime, str]:
    """Дата записи и её источник. Порядок: встреча → имя файла (с временем, затем без) → метаданные записи (аудио) /
    свойства документа (docx, xlsx, pptx, PDF, снимок) → дата загрузки. Дату из текста документа ставит разбор документа."""
    if meeting_started_at is not None:
        return _as_utc_naive(meeting_started_at), SOURCE_MEETING
    uploaded = upload_date or datetime.now(timezone.utc).replace(tzinfo=None)
    with_time = filename_datetime(original_filename)
    if with_time is not None:
        return with_time, SOURCE_FILENAME_TIME
    by_name = filename_date(original_filename, uploaded)
    if by_name is not None:
        # Точность «день»: полдень UTC, чтобы день не съезжал ни в одном поясе ±11 ч.
        return datetime(by_name.year, by_name.month, by_name.day, 12, 0), SOURCE_FILENAME
    if file_type == "audio" and file_path is not None:
        media = media_creation_time(file_path)
        if media is not None:
            return media, SOURCE_MEDIA
    meta = document_meta_date(file_path, file_type)
    if meta is not None:
        return meta, SOURCE_DOC_META
    return _as_utc_naive(uploaded), SOURCE_UPLOAD


def refine_from_text(artifact: Any, text: str) -> bool:
    """После разбора документа: дата из шапки главнее свойств файла и даты загрузки (ручную и из имени не трогаем)."""
    if getattr(artifact, "recorded_at_source", None) not in AUTO_REFINABLE or getattr(artifact, "file_type", "") == "audio":
        return False
    found = text_header_date(text, getattr(artifact, "upload_date", None))
    if found is None:
        return False
    artifact.recorded_at, artifact.recorded_at_source = datetime(found.year, found.month, found.day, 12, 0), SOURCE_DOC_TEXT
    return True


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


def material_date(artifact: Any) -> tuple[date | None, str]:
    """Дата материала для людей и правил: (дата, откуда). Нет фактической даты — дата загрузки с источником "upload"
    (решение владельца 2026-09-28: у каждого материала есть дата; в интерфейсе она честно подписана «дата загрузки»)."""
    explicit = recorded_local_date(artifact)
    if explicit is not None:
        return explicit, str(getattr(artifact, "recorded_at_source", "") or "")
    by_name = filename_date(getattr(artifact, "original_filename", None), getattr(artifact, "upload_date", None))
    if by_name is not None:
        return by_name, SOURCE_FILENAME
    uploaded = to_local(getattr(artifact, "upload_date", None))
    return (uploaded.date() if uploaded else None), SOURCE_UPLOAD


def manual_recorded_at(day: date) -> datetime:
    """Ручная дата записи: точность «день», полдень UTC (как у даты из имени файла)."""
    return datetime(day.year, day.month, day.day, 12, 0)


def manual_recorded_at_time(local: datetime, tz: timezone = LOCAL_TZ) -> datetime:
    """Ручная дата и время начала (Я5): местное время → UTC без пояса (как хранится recorded_at)."""
    aware = local if local.tzinfo is not None else local.replace(tzinfo=tz)
    return _as_utc_naive(aware)


def recorded_moment(artifact: Any) -> datetime | None:
    """Начало записи с точностью до минуты (встреча, метаданные, ручное время) в UTC с поясом; иначе None."""
    if getattr(artifact, "recorded_at_source", None) not in MINUTE_SOURCES or getattr(artifact, "recorded_at", None) is None:
        return None
    moment = artifact.recorded_at
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def item_moment(artifact: Any, value: dict) -> tuple[date | None, datetime | None]:
    """День и момент пункта (SOURCE-ANCHORS-PLAN этап 3): переписка — время своего сообщения; созвон с известным
    временем начала (Я3, Я5) — начало + таймкод; иначе только день материала. Непривязанный пункт переписки — (None, None):
    день первого сообщения выгрузки не его день."""
    if value.get("message_at"):
        try:
            at = datetime.fromisoformat(str(value["message_at"]))
        except ValueError:
            at = None
        if at is not None:
            at = at if at.tzinfo is not None else at.replace(tzinfo=LOCAL_TZ)
            if value.get("message_at_precision") == "day":  # копия чата без времени: известен только день (А9, Я3)
                return at.astimezone(LOCAL_TZ).date(), None
            return at.astimezone(LOCAL_TZ).date(), at
    if getattr(artifact, "recorded_at_source", None) == SOURCE_MESSAGES:
        return None, None
    start = recorded_moment(artifact)
    if start is not None and value.get("start_sec") is not None:
        at = start + timedelta(seconds=float(value["start_sec"]))
        return at.astimezone(LOCAL_TZ).date(), at
    return material_date(artifact)[0], None


def material_date_label(artifact: Any) -> str:
    """Подпись даты для LLM: «созвон 21.09.2026» или честное «дата загрузки 21.09.2026»."""
    explicit = recorded_local_date(artifact)
    if explicit is not None:
        if getattr(artifact, "recorded_at_source", None) == SOURCE_MESSAGES:
            return f"переписка с {explicit:%d.%m.%Y}"
        kind = "созвон" if getattr(artifact, "file_type", "") == "audio" else "дата материала"
        return f"{kind} {explicit:%d.%m.%Y}"
    by_name = filename_date(getattr(artifact, "original_filename", None), getattr(artifact, "upload_date", None))
    if by_name is not None:
        kind = "созвон" if getattr(artifact, "file_type", "") == "audio" else "дата материала"
        return f"{kind} {by_name:%d.%m.%Y}"
    uploaded = to_local(getattr(artifact, "upload_date", None))
    if uploaded is None:
        return ""
    unknown = "дата созвона неизвестна" if getattr(artifact, "file_type", "") == "audio" else "дата документа неизвестна"
    return f"дата загрузки {uploaded:%d.%m.%Y} ({unknown})"
