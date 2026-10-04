"""Даты материалов проекта: откуда взята дата записи (docs/plans/REQUIREMENTS-PLAN.md, этап 1).

    venv\\Scripts\\python scripts\\material_dates.py --project 18            # отчёт, база не меняется
    venv\\Scripts\\python scripts\\material_dates.py --project 18 --apply    # дозаполнить даты, найденные автоматически
    venv\\Scripts\\python scripts\\material_dates.py --all --apply           # все проекты

С --day-only — созвоны, у которых известен только день (порядок внутри дня не определён), с подсказкой по времени.
С --apply материалам без фактической даты (дата загрузки, только свойства файла или пусто) ставится дата так же, как
новым: встреча внутренней площадки (``встреча-N.wav`` → начало встречи N) → первое сообщение выгрузки переписки →
имя файла (со временем или без, «14 мая») → метаданные записи → шапка документа («Протокол от 19.08.2026») → свойства
файла (docx, xlsx, pptx, PDF, снимок) → дата загрузки (решение владельца 2026-09-28: у каждого материала есть дата,
в интерфейсе — «по дате загрузки»). Ручные даты и даты встреч не трогаются.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCE_LABELS = {"meeting": "встреча", "filename": "имя файла", "filename_time": "имя файла + время", "media": "метаданные записи",
                 "manual": "вручную", "manual_time": "вручную + время", "messages": "сообщения переписки",
                 "doc_text": "шапка документа", "doc_meta": "свойства файла", "upload": "дата загрузки"}
MEETING_FILE = re.compile(r"^встреча-(\d+)\.wav$", re.IGNORECASE)


def _meeting_date(db, art):
    """Запись встречи внутренней площадки, загруженная до автоматических дат: начало встречи по её номеру в имени."""
    from app.app.models.meeting import Meeting

    m = MEETING_FILE.match(art.original_filename or "")
    meeting = db.get(Meeting, int(m.group(1))) if m else None
    if meeting is None or meeting.project_id != art.project_id or meeting.started_at is None:
        return None
    return meeting.started_at


def _messages_date(settings, art):
    """Выгрузка переписки: первое сообщение (как у новой загрузки, ``chat_export.ingest_telegram_export``)."""
    from datetime import timezone

    from app.app.services.chat_export import load_artifact_export, period

    export = load_artifact_export(settings, art)
    first = period(export)[0] if export else None
    return first.astimezone(timezone.utc).replace(tzinfo=None) if first is not None else None


def _auto_date(db, settings, art, text: str):
    """(recorded_at, source) по тем же правилам, что у новой загрузки; последняя ступень — дата загрузки."""
    from app.app.services import recorded_at as ra

    started = _meeting_date(db, art)
    if started is not None:
        return ra._as_utc_naive(started), ra.SOURCE_MEETING
    first = _messages_date(settings, art)
    if first is not None:
        return first, ra.SOURCE_MESSAGES
    recorded, source = ra.resolve_recorded_at(original_filename=art.original_filename, file_path=Path(art.file_path) if art.file_path else None,
                                              file_type=art.file_type, upload_date=art.upload_date)
    if source in (ra.SOURCE_FILENAME, ra.SOURCE_FILENAME_TIME, ra.SOURCE_MEDIA):
        return recorded, source
    if art.file_type != "audio":
        found = ra.text_header_date(text, art.upload_date)
        if found is not None:
            return ra.manual_recorded_at(found), ra.SOURCE_DOC_TEXT
    return recorded, source  # свойства файла или дата загрузки


def fill_dates(db, settings, project_id: int, *, apply: bool, out=print) -> dict[str, int]:
    """Отчёт по датам материалов проекта; с ``apply`` — записать найденные даты. Вернуть {материалов, поставлено, по загрузке}."""
    from sqlalchemy import select

    from app.app.models.artifact import Artifact
    from app.app.services import recorded_at as ra

    arts = db.scalars(select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.id)).all()
    stats = {"materials": len(arts), "changed": 0, "by_upload": 0}
    for art in arts:
        proposal = None
        if art.recorded_at_source in ra.AUTO_REFINABLE:
            proposal = _auto_date(db, settings, art, art.transcription_text or "")
            if (proposal[1], proposal[0]) == (art.recorded_at_source, art.recorded_at):
                proposal = None
        if proposal and apply:
            ra.set_recorded(art, *proposal)
            stats["changed"] += 1
        day, source = ra.material_date(art)
        stats["by_upload"] += source == ra.SOURCE_UPLOAD
        uploaded = ra.to_local(art.upload_date)
        kind = "созвон" if art.file_type == "audio" else "документ"
        mismatch = " (загружен как созвон)" if kind == "документ" and art.source_type == "call" else ""
        hint = f"  → можно: {ra.to_local(proposal[0]):%d.%m.%Y} ({SOURCE_LABELS.get(proposal[1], proposal[1])})" if proposal and not apply else ""
        out(f"{art.id:>5}  {day.strftime('%d.%m.%Y') if day else 'неизвестна ':<11}  {SOURCE_LABELS.get(source, source):<20}  "
            f"загружен {uploaded:%d.%m.%Y}  {kind}{mismatch}  {art.original_filename}{hint}")
    if apply:
        db.commit()
    return stats


def day_only_report(db, project_id: int, out=print) -> int:
    """Созвоны проекта, у которых известен только день (ПФ6): порядок внутри дня не определён. Подсказка — верхняя граница
    начала: время загрузки минус длительность (запись не могла начаться позже). Время начала подтверждает человек в карточке материала."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.app.models.artifact import Artifact
    from app.app.services import recorded_at as ra

    arts = [a for a in db.scalars(select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.id)).all()
            if a.file_type == "audio" and ra.recorded_precision(a) == ra.PRECISION_DAY]
    by_day: dict = {}
    for art in arts:
        day, _source = ra.material_date(art)
        by_day.setdefault(day, []).append(art)
    for day, group in sorted(by_day.items(), key=lambda kv: (kv[0] is None, kv[0])):
        warn = f"  ← {len(group)} созвона в один день, порядок неизвестен" if len(group) > 1 else ""
        out(f"{day.strftime('%d.%m.%Y') if day else 'дата неизвестна'}{warn}")
        for art in group:
            late = ""
            if art.upload_date and art.duration_sec and ra.to_local(art.upload_date).date() == day:  # загрузка в тот же день — граница осмысленна
                latest = ra.to_local(art.upload_date) - timedelta(seconds=float(art.duration_sec))
                late = f"; начало не позже {latest:%d.%m.%Y %H:%M} (загружен {ra.to_local(art.upload_date):%d.%m %H:%M}, длительность {int(art.duration_sec // 60)} мин)"
            out(f"   #{art.id} {art.original_filename} — источник «{SOURCE_LABELS.get(art.recorded_at_source, art.recorded_at_source)}»{late}")
    return len(arts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--project", type=int)
    group.add_argument("--all", action="store_true", help="все проекты")
    ap.add_argument("--apply", action="store_true", help="записать найденные даты (без флага — только отчёт)")
    ap.add_argument("--day-only", action="store_true", help="показать созвоны, у которых известен только день (ничего не меняет)")
    args = ap.parse_args()

    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.project import Project

    settings = get_settings()
    db = SessionLocal()
    try:
        ids = [args.project] if args.project else list(db.scalars(select(Project.id).order_by(Project.id)).all())
        for project_id in ids:
            if args.all:
                print(f"\n=== #{project_id} {db.get(Project, project_id).name}")
            if args.day_only:
                print(f"созвонов только с днём: {day_only_report(db, project_id)}")
                continue
            stats = fill_dates(db, settings, project_id, apply=args.apply)
            print(f"материалов: {stats['materials']}, по дате загрузки: {stats['by_upload']}"
                  + (f", дат поставлено: {stats['changed']}" if args.apply else ""))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
