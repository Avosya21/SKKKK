"""Память решений проекта: акты -> предметы -> события -> итоги (docs/plans/DECISION-MEMORY-PLAN.md, Э2–Э5).

    venv\\Scripts\\python scripts\\build_decision_memory.py --project 18                      # всё по порядку
    venv\\Scripts\\python scripts\\build_decision_memory.py --project 18 --steps subjects events judge   # без разбора реплик
    venv\\Scripts\\python scripts\\build_decision_memory.py --project 18 --rebuild-subjects  # предметы заново (кроме решённых человеком)

Шаги: ``acts`` — речевые акты реплик (модель, самый долгий), ``subjects`` — предметы (векторы + редкие вопросы модели), ``events`` —
события журнала из актов (код), ``judge`` — итоги предметов (модель). Запускать, когда очередь воркера пуста. Перед записью — копия БД
в ``data/backups/``. После — строка в табло: ``scripts/memory_scorecard.py --project <N> --stage ...``.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STEPS = ("acts", "subjects", "events", "judge")


def _backup(db_path: Path) -> Path:
    target_dir = db_path.parent / "backups"
    target_dir.mkdir(exist_ok=True)
    target = target_dir / f"app_{datetime.now():%Y%m%d_%H%M%S}_before_decision_memory.db"
    src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--steps", nargs="*", choices=STEPS, default=list(STEPS))
    ap.add_argument("--artifact", type=int, nargs="*", default=[], help="для шага acts: только эти материалы")
    ap.add_argument("--rebuild-subjects", action="store_true")
    ap.add_argument("--legacy-subjects", action="store_true", help="старая жадная раскладка по векторам вместо прохода модели по окну")
    args = ap.parse_args(argv)

    from app.app.config import get_settings
    from app.app.db import Base, SessionLocal, apply_schema_migrations, get_engine
    import app.app.models  # noqa: F401
    from app.app.services import decision_log, speech_acts, subject_judge, subject_windows, subjects

    settings = get_settings()
    if settings.database_url.startswith("sqlite:///"):
        path = Path(settings.database_url.replace("sqlite:///", "", 1))
        if path.exists():
            print("Копия БД:", _backup(path))
    Base.metadata.create_all(get_engine())
    apply_schema_migrations()
    db = SessionLocal()
    started = time.monotonic()
    try:
        if "acts" in args.steps:
            r = speech_acts.extract_project(db, settings, args.project, artifact_ids=args.artifact or None, progress=print)
            print(f"[acts] актов {r['acts']}, ошибок окон {r['errors']}")
        if "subjects" in args.steps:
            assign = subjects.assign if args.legacy_subjects else subject_windows.assign
            r = assign(db, settings, args.project, rebuild=args.rebuild_subjects)
            print(f"[subjects] {r}")
        if "events" in args.steps:
            r = subjects.build_events(db, args.project)
            print(f"[events] {r}")
        if "judge" in args.steps:
            r = subject_judge.judge_project(db, settings, args.project, progress=print)
            print(f"[judge] {r}")
        else:
            from sqlalchemy import select

            from app.app.models.decision_memory import Subject

            for s in db.scalars(select(Subject).where(Subject.project_id == args.project)).all():
                decision_log.refresh_subject(db, s)
            db.commit()
    finally:
        db.close()
    print(f"Готово за {round(time.monotonic() - started)} с.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
