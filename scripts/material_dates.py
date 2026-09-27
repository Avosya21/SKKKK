"""Даты материалов проекта: откуда взята дата записи (docs/plans/REQUIREMENTS-PLAN.md, этап 1).

    venv\\Scripts\\python scripts\\material_dates.py --project 18            # отчёт, база не меняется
    venv\\Scripts\\python scripts\\material_dates.py --project 18 --apply    # дозаполнить даты, найденные автоматически

С --apply материалам с датой загрузки (или только из свойств файла) ставится дата так же, как новым: имя файла (со временем
или без) → метаданные записи → шапка документа («Протокол от 19.08.2026») → свойства файла (docx, xlsx, pptx, PDF, снимок).
Ручные даты, даты встреч и переписки не трогаются. «неизвестна» — поставьте дату на странице проекта.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCE_LABELS = {"meeting": "встреча", "filename": "имя файла", "filename_time": "имя файла + время", "media": "метаданные записи",
                 "manual": "вручную", "manual_time": "вручную + время", "messages": "сообщения переписки",
                 "doc_text": "шапка документа", "doc_meta": "свойства файла", "upload": "—"}


def _auto_date(art, text: str):
    """(recorded_at, source) по тем же правилам, что у новой загрузки, или None."""
    from app.app.services import recorded_at as ra

    recorded, source = ra.resolve_recorded_at(original_filename=art.original_filename, file_path=Path(art.file_path) if art.file_path else None,
                                              file_type=art.file_type, upload_date=art.upload_date)
    if source in (ra.SOURCE_FILENAME, ra.SOURCE_FILENAME_TIME, ra.SOURCE_MEDIA):
        return recorded, source
    if art.file_type != "audio":
        found = ra.text_header_date(text, art.upload_date)
        if found is not None:
            return ra.manual_recorded_at(found), ra.SOURCE_DOC_TEXT
    if source == ra.SOURCE_DOC_META:
        return recorded, source
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="записать найденные даты (без флага — только отчёт)")
    args = ap.parse_args()

    from sqlalchemy import select

    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.services import recorded_at as ra

    db = SessionLocal()
    try:
        arts = db.scalars(select(Artifact).where(Artifact.project_id == args.project).order_by(Artifact.id)).all()
        unknown = changed = 0
        for art in arts:
            proposal = None
            if art.recorded_at_source in ra.AUTO_REFINABLE:
                proposal = _auto_date(art, art.transcription_text or "")
                if proposal and (proposal[1], proposal[0]) == (art.recorded_at_source, art.recorded_at):
                    proposal = None
            if proposal and args.apply:
                art.recorded_at, art.recorded_at_source = proposal
                changed += 1
            day, source = ra.material_date(art)
            unknown += day is None
            uploaded = ra.to_local(art.upload_date)
            kind = "созвон" if art.file_type == "audio" else "документ"
            mismatch = " (загружен как созвон)" if kind == "документ" and art.source_type == "call" else ""
            hint = f"  → можно: {ra.to_local(proposal[0]):%d.%m.%Y} ({SOURCE_LABELS.get(proposal[1], proposal[1])})" if proposal and not args.apply else ""
            print(f"{art.id:>5}  {day.strftime('%d.%m.%Y') if day else 'неизвестна ':<11}  {SOURCE_LABELS.get(source, source):<20}  "
                  f"загружен {uploaded:%d.%m.%Y}  {kind}{mismatch}  {art.original_filename}{hint}")
        if args.apply:
            db.commit()
        print(f"\nматериалов: {len(arts)}, без даты записи: {unknown}" + (f", дат поставлено: {changed}" if args.apply else ""))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
