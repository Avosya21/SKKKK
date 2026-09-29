"""Разобрать уже загруженные выгрузки переписки Telegram на сообщения (docs/plans/SOURCE-ANCHORS-PLAN.md, этап 1).

    venv\\Scripts\\python scripts\\chat_exports.py --project 18            # отчёт: сообщений, период, пересечения
    venv\\Scripts\\python scripts\\chat_exports.py --project 18 --apply    # записать сообщения и новый текст

С --apply: ``<artifact_id>_messages.json``, текст «**день**» / «**ЧЧ:ММ Автор:** текст» вместо прежнего (прежний —
копией ``_clean.before_chat.txt``), дата материала — по сообщениям; в очередь ставится переиндексация фрагментов.
Пункты переписки после этого переизвлекаются кнопкой «Переизвлечь требования» (ручные решения переносятся).

Копии чата Битрикс24 (``.txt``: «[Имя]», «{Цитата}…{Конец цитаты}», DECISIONS 2026-09-28) разбираются так же: сообщения
с автором и ответами, у сообщений только день (дата материала не меняется), текст «[Автор] (в ответ на …)» без повтора
цитат.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="записать (без флага — только отчёт)")
    args = ap.parse_args()

    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.services import chat_export as ce
    from app.app.services.queue import enqueue_knowledge_refresh
    from app.app.services.storage import parsed_dir

    settings = get_settings()
    db = SessionLocal()
    try:
        arts = db.scalars(select(Artifact).where(Artifact.project_id == args.project, Artifact.file_type == "html").order_by(Artifact.upload_date, Artifact.id)).all()
        found = []
        for art in arts:
            path = Path(art.file_path)
            if not path.is_file():
                continue
            html = path.read_text(encoding="utf-8", errors="replace")
            if not ce.looks_like_telegram_export(html):
                continue
            export = ce.parse_telegram_html(html)
            first, last = ce.period(export)
            found.append((art, export))
            print(json.dumps({"artifact": art.id, "file": art.original_filename, "title": export["title"], "messages": len(export["messages"]),
                              "from": first.isoformat() if first else None, "to": last.isoformat() if last else None}, ensure_ascii=False))
        for i, (art, export) in enumerate(found):
            ids = {m["id"] for m in export["messages"]}
            for other, other_export in found[:i]:
                if other_export["title"] == export["title"]:
                    common = ids & {m["id"] for m in other_export["messages"]}
                    if common:
                        print(f"пересечение {other.id} ↔ {art.id}: {len(common)} сообщений ({min(common)}–{max(common)}) — источник в {other.id} (Я1)")
        from app.app.services.pipeline_documents import _extract_plain
        from app.app.services.recorded_at import material_date

        notes = db.scalars(select(Artifact).where(Artifact.project_id == args.project, Artifact.file_type == "txt").order_by(Artifact.upload_date, Artifact.id)).all()
        copies = []
        for art in notes:
            path = Path(art.file_path)
            if not path.is_file():
                continue
            text = _extract_plain(path)
            if not ce.looks_like_bitrix_copy(text):
                continue
            export = ce.parse_bitrix_copy(text, material_date(art)[0])
            copies.append((art, text))
            replies = sum(1 for m in export["messages"] if m.get("reply_to") is not None)
            print(json.dumps({"artifact": art.id, "file": art.original_filename, "format": "bitrix_copy", "messages": len(export["messages"]),
                              "replies": replies, "day": export["messages"][0]["at"][:10] if export["messages"] else None}, ensure_ascii=False))
        if not args.apply:
            print("только отчёт; для записи добавьте --apply")
            return 0
        for art, text in copies:
            folder = parsed_dir(settings, art.project_id, art.source_type)
            clean = folder / f"{art.artifact_id}_clean.txt"
            if clean.is_file() and not (folder / f"{art.artifact_id}_clean.before_chat.txt").is_file():
                shutil.copy2(clean, folder / f"{art.artifact_id}_clean.before_chat.txt")
            rendered = ce.ingest_bitrix_copy(settings, art, text)
            if rendered is None:
                continue
            clean.write_text(rendered, encoding="utf-8")
            (folder / f"{art.artifact_id}_raw.txt").write_text(rendered, encoding="utf-8")
            art.transcription_text = rendered
            art.transcription_file_path = str(clean)
            db.commit()
            job = enqueue_knowledge_refresh(db, art.id)
            print(f"{art.id} {art.original_filename}: записано, переиндексация {'поставлена' if job else 'уже в очереди'}")
        for art, _export in found:
            html = Path(art.file_path).read_text(encoding="utf-8", errors="replace")
            folder = parsed_dir(settings, art.project_id, art.source_type)
            clean = folder / f"{art.artifact_id}_clean.txt"
            if clean.is_file() and not (folder / f"{art.artifact_id}_clean.before_chat.txt").is_file():
                shutil.copy2(clean, folder / f"{art.artifact_id}_clean.before_chat.txt")
            text = ce.ingest_telegram_export(settings, art, html)
            if text is None:
                continue
            clean.write_text(text, encoding="utf-8")
            (folder / f"{art.artifact_id}_raw.txt").write_text(text, encoding="utf-8")
            art.transcription_text = text
            art.transcription_file_path = str(clean)
            db.commit()
            job = enqueue_knowledge_refresh(db, art.id)
            print(f"{art.id} {art.original_filename}: записано, переиндексация {'поставлена' if job else 'уже в очереди'}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
