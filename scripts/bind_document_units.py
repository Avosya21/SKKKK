"""Привязать уже извлечённые пункты документов к абзацам P<n> без модели (docs/plans/DECISION-MEMORY-PLAN.md, Э1).

    venv\\Scripts\\python scripts\\bind_document_units.py --project 18            # отчёт, база не меняется
    venv\\Scripts\\python scripts\\bind_document_units.py --project 18 --apply    # записать абзац и цитату в пункты

Перед записью делается копия БД в ``data/backups/``. Правятся только ключи ``source_*`` пункта; решения человека и проверки
модели не затрагиваются. Пункты, у которых слов пункта в единице меньше половины, остаются без абзаца (их источник уточнит
переизвлечение по промпту v5: «Фоновая работа» → «Переизвлечь» для выбранных документов).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _backup(db_path: Path) -> Path:
    target_dir = db_path.parent / "backups"
    target_dir.mkdir(exist_ok=True)
    target = target_dir / f"app_{datetime.now():%Y%m%d_%H%M%S}_before_doc_units.db"
    src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rebind", action="store_true", help="заново и у пунктов с абзацем (нумерация единиц документа изменилась)")
    ap.add_argument("--artifact", type=int, action="append", help="только эти документы (id материала, можно несколько раз)")
    args = ap.parse_args(argv)

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    import app.app.models  # noqa: F401
    from app.app.services import doc_anchors

    settings = get_settings()
    if args.apply and settings.database_url.startswith("sqlite:///"):
        path = Path(settings.database_url.replace("sqlite:///", "", 1))
        if path.exists():
            print("Копия БД:", _backup(path))
    db = SessionLocal()
    try:
        report = doc_anchors.bind_project(db, settings, args.project, apply=args.apply, rebind=args.rebind,
                                          artifact_ids=set(args.artifact) if args.artifact else None)
    finally:
        db.close()
    verb = "привязано" if args.apply else "можно привязать"
    for doc in report["documents"]:
        print(f"  #{doc['artifact_id']} {doc['name'][:50]}: пунктов {doc['items']}, уже с абзацем {doc['already']}, {verb} {doc['bound']}, без абзаца {doc['left']}")
    t = report["total"]
    print(f"Итого: пунктов {t['items']}, с абзацем {t['already']}, {verb} {t['bound']}, без абзаца {t['left']}")
    if not args.apply:
        print("Это отчёт. Записать: добавьте --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
