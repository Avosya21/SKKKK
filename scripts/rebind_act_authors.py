"""Имена из стенограмм созвонов — в акты и события памяти решений (``services/act_authors.py``), без модели.

    venv\\Scripts\\python scripts\\rebind_act_authors.py --dry-run                 # только посчитать, БД не меняется
    venv\\Scripts\\python scripts\\rebind_act_authors.py                           # все созвоны с актами
    venv\\Scripts\\python scripts\\rebind_act_authors.py --project 28 --artifact art_2026-10-16_163603_c3b375

Нужен для созвонов, где голоса опознали после построения памяти решений: акты остались с ``speaker_N`` и без человека.
Новые созвоны обновляются сами при подтверждении голосов (``entities.rebind_call_entities``). Перед записью — копия БД
в ``data/backups/``.
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
    target = target_dir / f"app_{datetime.now():%Y%m%d_%H%M%S}_before_act_authors.db"
    src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int, help="только этот проект")
    ap.add_argument("--artifact", nargs="*", default=[], help="только эти созвоны (artifact_id)")
    ap.add_argument("--dry-run", action="store_true", help="посчитать и откатить")
    args = ap.parse_args(argv)

    from sqlalchemy import func, select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    import app.app.models  # noqa: F401
    from app.app.models.artifact import Artifact
    from app.app.models.speech_act import SpeechAct
    from app.app.services.act_authors import rebind_call_authors
    from app.app.services.transcript_turns import PLACEHOLDER_SPEAKER

    settings = get_settings()
    if not args.dry_run and settings.database_url.startswith("sqlite:///"):
        path = Path(settings.database_url.replace("sqlite:///", "", 1))
        if path.exists():
            print("Копия БД:", _backup(path))
    db = SessionLocal()
    total = {"acts": 0, "people": 0, "events": 0, "subjects": 0}
    try:
        q = (select(Artifact).where(Artifact.file_type == "audio", Artifact.id.in_(select(SpeechAct.artifact_id).distinct()))
             .order_by(Artifact.project_id, Artifact.id))
        if args.project is not None:
            q = q.where(Artifact.project_id == args.project)
        if args.artifact:
            q = q.where(Artifact.artifact_id.in_(args.artifact))
        for art in db.scalars(q).all():
            r = rebind_call_authors(db, settings, art)
            if args.dry_run:
                db.flush()
            else:
                db.commit()
            left = sum(1 for (a,) in db.execute(select(SpeechAct.author).where(SpeechAct.artifact_id == art.id))
                       if PLACEHOLDER_SPEAKER.match((a or "").strip()))
            if any(r.values()) or left:
                n = db.scalar(select(func.count()).select_from(SpeechAct).where(SpeechAct.artifact_id == art.id))
                print(f"пр.{art.project_id} {art.artifact_id}: актов {n}, автор исправлен {r['acts']}, с человеком {r['people']}, "
                      f"событий {r['events']}, предметов {r['subjects']}, осталось меток голоса {left}")
            for k in total:
                total[k] += r[k]
        if args.dry_run:
            db.rollback()
    finally:
        db.close()
    print(("Проверка (без записи): " if args.dry_run else "Итого: ")
          + f"автор исправлен у {total['acts']} актов, человек у {total['people']}, событий {total['events']}, "
            f"предметов пересчитано {total['subjects']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
