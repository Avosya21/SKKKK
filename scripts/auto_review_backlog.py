"""Проверить моделью всё накопленное в проекте (этап 5 docs/plans/AUTO-REVIEW-PLAN.md) — ставит фоновые задачи в очередь.

Модель вызывает воркер (низкий приоритет: расшифровка всегда раньше). Сначала — тень, после разметки — live:
live повторно модель не зовёт там, где уже есть теневой вердикт той же версии промпта.

    venv\\Scripts\\python scripts\\auto_review_backlog.py --project 35                       # режим проекта (по умолчанию shadow)
    venv\\Scripts\\python scripts\\auto_review_backlog.py --project 35 --mode live --scope requirements
    venv\\Scripts\\python scripts\\auto_review_backlog.py --all --dry-list                    # только посчитать

Ставит задачи, только если запущенный воркер понимает ``auto_review`` (иначе старый воркер переобработал бы созвоны).
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
    target.add_argument("--all", action="store_true", help="все активные проекты")
    ap.add_argument("--scope", choices=("all", "requirements", "conflicts"), default="all")
    ap.add_argument("--mode", choices=("shadow", "live"))
    ap.add_argument("--dry-list", action="store_true", help="только посчитать, ничего не ставить")
    args = ap.parse_args()

    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.services.auto_review import SECONDS_PER_CONFLICT, SECONDS_PER_REQUIREMENT, BacklogError, queue_backlog
    from app.app.services.review_conflicts import pending_links
    from app.app.services.review_requirements import _pending

    settings = get_settings()
    db = SessionLocal()
    try:
        ids = [args.project] if args.project else [p.id for p in db.scalars(select(Project).where(Project.is_active.is_(True)))]
        code = 0
        for pid in ids:
            if args.dry_list:
                arts = db.scalars(select(Artifact).where(Artifact.project_id == pid, Artifact.status == "done")).all()
                reqs = sum(len(_pending(db, a)) for a in arts)
                confs = len(pending_links(db, project_id=pid))
                minutes = round((reqs * SECONDS_PER_REQUIREMENT + confs * SECONDS_PER_CONFLICT) / 60, 1)
                print(json.dumps({"project_id": pid, "artifacts": len(arts), "requirements": reqs, "conflicts": confs,
                                  "estimate_minutes": minutes}, ensure_ascii=False))
                continue
            try:
                print(json.dumps(queue_backlog(db, settings, pid, scope=args.scope, mode=args.mode), ensure_ascii=False))
            except BacklogError as exc:
                print(f"проект {pid}: {exc}", file=sys.stderr)
                code = 2
        db.rollback()
        return code
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
