"""Разовая чистка конфликтов после исправления ошибок графа (docs/plans/CONFLICT-AUTONOMY-PLAN.md, этап 1).

По умолчанию — только отчёт, база не меняется:
    venv\\Scripts\\python scripts\\fix_conflict_links.py --project 30
    venv\\Scripts\\python scripts\\fix_conflict_links.py --all
Схлопнуть обратные дубли A→B / B→A:
    venv\\Scripts\\python scripts\\fix_conflict_links.py --project 30 --apply
Дополнительно снять старые авторазрешения по фразе отмены (вернуть конфликты в очередь новой проверки):
    venv\\Scripts\\python scripts\\fix_conflict_links.py --project 30 --apply --release-legacy
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--project", type=int)
    target.add_argument("--all", action="store_true", help="все проекты")
    ap.add_argument("--apply", action="store_true", help="записать изменения (без флага — только отчёт)")
    ap.add_argument("--release-legacy", action="store_true", help="с --apply: снять старые авторазрешения по фразе отмены")
    args = ap.parse_args()

    from sqlalchemy import select

    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.services.graph import repair_conflict_links

    db = SessionLocal()
    try:
        ids = [args.project] if args.project else list(db.scalars(select(Project.id).order_by(Project.id)).all())
        for pid in ids:
            report = repair_conflict_links(db, pid, apply=args.apply, release_legacy=args.release_legacy)
            if report["duplicate_pairs"] or report["legacy_auto"]:
                print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.apply:
            db.commit()
        else:
            db.rollback()
            print("только отчёт; для записи добавьте --apply")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
