"""Сравнение памяти решений с прежним контуром актуальности пунктов (docs/plans/DECISION-MEMORY-STAGES-6-8.md, Э7).

    venv\\Scripts\\python scripts\\compare_memory_vs_review.py --project 18

Для каждого пункта проекта с репликой-источником берёт состояние прежнего контура и состояние по памяти; спорные пункты решает
арбитр-модель (человек ничего не размечает). Результат пишется в ``memory_shadow_review``; печатаются счётчики, готовность проекта
к режиму live и список расхождений с вердиктами. Запускать, когда очередь воркера пуста (нужна модель). Копия БД перед записью.
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
    target = target_dir / f"app_{datetime.now():%Y%m%d_%H%M%S}_before_memory_compare.db"
    src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int, required=True)
    args = ap.parse_args(argv)

    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import Base, SessionLocal, apply_schema_migrations, get_engine
    import app.app.models  # noqa: F401
    from app.app.models.decision_memory import MemoryShadowReview, Subject
    from app.app.services import memory_compare, memory_mode

    settings = get_settings()
    if settings.database_url.startswith("sqlite:///"):
        path = Path(settings.database_url.replace("sqlite:///", "", 1))
        if path.exists():
            print("Копия БД:", _backup(path))
    Base.metadata.create_all(get_engine())
    apply_schema_migrations()
    db = SessionLocal()
    try:
        stats = memory_compare.compare_project(db, settings, args.project)
        print("Сравнение:", stats)
        for row in db.scalars(select(MemoryShadowReview).where(MemoryShadowReview.project_id == args.project, MemoryShadowReview.verdict != "agree")).all():
            subject = db.get(Subject, row.subject_id) if row.subject_id else None
            print(f"  пункт {row.entity_id}: прежний {row.old_state}, память {row.new_state}, прав: {row.verdict} — {(subject.title if subject else '')[:60]} | {row.reason or ''}")
        ready = memory_mode.readiness(db, args.project)
        for c in ready["checks"]:
            print(f"  [{'ok' if c['ok'] else '--'}] {c['title']}: {c['detail']}")
        print("Готов к live:", ready["ready"])
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
