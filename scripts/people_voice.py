"""Согласие внешнего человека на голос: отзыв делается только здесь (PLATFORM-FOUNDATION-PLAN, Д4/Д5).

    venv\Scripts\python scripts\people_voice.py list --project 18
    venv\Scripts\python scripts\people_voice.py give --project 18 --person 3 --by 28 --basis "договор"
    venv\Scripts\python scripts\people_voice.py withdraw --project 18 --person 3

Отзыв удаляет слепки голоса этого человека в проекте и снимает отметку о согласии; перед удалением делается копия БД
в ``data/backups/``. Отметка «согласие получено» ставится в портале (карточка «Люди и стороны»).
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
    target = target_dir / f"app_{datetime.now():%Y%m%d_%H%M%S}_before_voice_withdraw.db"
    src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "give", "withdraw"):
        p = sub.add_parser(name)
        p.add_argument("--project", type=int, required=True)
        if name in ("give", "withdraw"):
            p.add_argument("--person", type=int, required=True, nargs="+" if name == "give" else None)
        if name == "give":
            p.add_argument("--by", type=int, required=True, help="номер учётки того, кто подтверждает получение согласия")
            p.add_argument("--basis", required=True, help="на чём основано согласие (например, пункт договора)")
    args = ap.parse_args(argv)

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    import app.app.models  # noqa: F401
    from app.app.services import people

    db = SessionLocal()
    try:
        if args.cmd == "list":
            for p in people.list_people(db, args.project):
                state = "сотрудник (согласие в карточке)" if p.user_id else ("согласие получено " + p.voice_consent_at.strftime("%Y-%m-%d") if p.voice_consent_at else "согласия нет")
                print(f"  #{p.id} {p.display_name}: {state}")
            return 0
        if args.cmd == "give":
            for person_id in args.person:
                person = people.give_voice_consent(db, args.project, person_id, by_user_id=args.by, basis=args.basis)
                print(f"  согласие отмечено: #{person.id} {person.display_name} ({args.basis})")
            db.commit()
            return 0
        url = get_settings().database_url
        if url.startswith("sqlite:///"):
            path = Path(url.replace("sqlite:///", "", 1))
            if path.exists():
                print("Копия БД:", _backup(path))
        deleted = people.withdraw_voice_consent(db, args.project, args.person)
        db.commit()
        print(f"Согласие снято, удалено слепков голоса: {deleted}")
        return 0
    except people.PeopleError as exc:
        db.rollback()
        print("Ошибка:", exc)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
