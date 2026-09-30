"""Первичное наполнение реестра людей проекта (docs/plans/PLATFORM-FOUNDATION-PLAN.md, ПФ2).

    venv\Scripts\python scripts\seed_people.py --project 18           # черновик: кого система нашла и какую сторону видит
    venv\Scripts\python scripts\seed_people.py --project 18 --apply   # записать людей, написания и вопросы «Кто это?»

Перед записью делается копия БД в ``data/backups/``. Повторный запуск безопасен: найденные люди дополняются
написаниями, решения человека (сторона, объединения) слабее не становятся.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SIDE_RU = {"executor": "исполнитель", "customer": "заказчик", "unknown": "не определена"}


def backup_db(db_path: Path) -> Path:
    target_dir = db_path.parent / "backups"
    target_dir.mkdir(exist_ok=True)
    target = target_dir / f"app_{datetime.now():%Y%m%d_%H%M%S}_before_people_seed.db"
    src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="записать (без флага — только отчёт)")
    args = ap.parse_args(argv)

    from app.app.config import get_settings
    from app.app.db import Base, SessionLocal, apply_schema_migrations, get_engine
    import app.app.models  # noqa: F401
    from app.app.services import people_seed

    settings = get_settings()
    if args.apply and settings.database_url.startswith("sqlite:///"):
        db_file = Path(settings.database_url.replace("sqlite:///", "", 1))
        if db_file.exists():
            print("Копия БД:", backup_db(db_file))
    Base.metadata.create_all(get_engine())
    apply_schema_migrations()
    db = SessionLocal()
    try:
        report = people_seed.seed_project(db, settings, args.project, apply=args.apply)
    finally:
        db.close()
    print(f"Людей найдено: {len(report['people'])}" + (f"; создано {report['created']}, уже были {report['skipped_existing']}" if args.apply else ""))
    for row in report["people"]:
        acc = f", учётка #{row['user_id']}" if row["user_id"] else ""
        print(f"  - {row['name']} [{SIDE_RU[row['side']]}, источник: {row['side_source']}{acc}] написания: {', '.join(row['aliases'])}; встречается: {', '.join(row['seen_in'])}")
    if report["unknown"]:
        print("Неоднозначные одиночные имена (спросит «Кто это?»):")
        for u in report["unknown"]:
            print(f"  - {u['label']} ({u['where']}): подходит {', '.join(u['candidates']) or 'никто'}")
    if not args.apply:
        print("Это черновик. Записать: добавьте --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
