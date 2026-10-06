"""Речевые акты реплик переписки и созвонов проекта (docs/plans/DECISION-MEMORY-PLAN.md, Э2).

    venv\\Scripts\\python scripts\\extract_speech_acts.py --project 18                     # все материалы с репликами
    venv\\Scripts\\python scripts\\extract_speech_acts.py --project 18 --artifact 9 44     # только эти материалы
    venv\\Scripts\\python scripts\\extract_speech_acts.py --project 18 --report            # без модели: что уже в таблице

Вызывает локальную модель (по окну реплик): запускать, когда очередь воркера пуста. Прежние акты материала заменяются.
Перед записью делается копия БД в ``data/backups/``.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _backup(db_path: Path) -> Path:
    target_dir = db_path.parent / "backups"
    target_dir.mkdir(exist_ok=True)
    target = target_dir / f"app_{datetime.now():%Y%m%d_%H%M%S}_before_speech_acts.db"
    src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--artifact", type=int, nargs="*", default=[])
    ap.add_argument("--report", action="store_true", help="показать, что уже в таблице, модель не вызывать")
    args = ap.parse_args(argv)

    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import Base, SessionLocal, apply_schema_migrations, get_engine
    import app.app.models  # noqa: F401
    from app.app.models.speech_act import ACT_LABELS, SpeechAct
    from app.app.services import speech_acts

    settings = get_settings()
    Base.metadata.create_all(get_engine())
    apply_schema_migrations()
    db = SessionLocal()
    try:
        if not args.report:
            if settings.database_url.startswith("sqlite:///"):
                path = Path(settings.database_url.replace("sqlite:///", "", 1))
                if path.exists():
                    print("Копия БД:", _backup(path))
            result = speech_acts.extract_project(db, settings, args.project, artifact_ids=args.artifact or None, progress=print)
            print(f"Итого актов: {result['acts']}, ошибок окон: {result['errors']}")
        rows = db.scalars(select(SpeechAct).where(SpeechAct.project_id == args.project)).all()
        counts = Counter(r.act for r in rows)
        print("В таблице:", ", ".join(f"{ACT_LABELS[a]} {n}" for a, n in counts.most_common()) or "пусто")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
