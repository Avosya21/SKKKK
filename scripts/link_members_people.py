"""Участники проектов → люди проектов (docs/plans/PEOPLE-FLOW-PLAN.md, ЛГ3).

    venv\\Scripts\\python.exe scripts\\link_members_people.py           # план (база не меняется)
    venv\\Scripts\\python.exe scripts\\link_members_people.py --apply   # применить; копия БД в data/backups/
    venv\\Scripts\\python.exe scripts\\link_members_people.py --skip-project 35   # без этих проектов

До ЛГ3 выдача доступа к проекту не заводила человека в «Люди и стороны». Для каждого участника без человека:
есть человек без учётки с тем же именем, что в карточке, — ему привязывается учётка; иначе новый человек по карточке
(``people.member_person``). Без карточки (нет фамилии и имени) — «нужна карточка», такие не трогаются.
Имена — только в консоль (не в git). Повторный запуск ничего не меняет.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def plan(db, skip: set[int] | None = None) -> list[dict]:
    """Участники без человека проекта и что с ними будет: link (есть человек с тем же именем) | create | no_card."""
    from sqlalchemy import select

    from app.app.models.person import Person
    from app.app.models.project import Project
    from app.app.models.project_member import ProjectMember
    from app.app.models.user import User
    from app.app.services import people
    from app.app.services import user_profile as profiles

    out: list[dict] = []
    active = set(db.scalars(select(Project.id).where(Project.is_active.is_(True))).all())
    for m in db.scalars(select(ProjectMember).order_by(ProjectMember.project_id, ProjectMember.user_id)).all():
        if m.project_id not in active or (skip and m.project_id in skip):
            continue
        if db.scalar(select(Person.id).where(Person.project_id == m.project_id, Person.user_id == m.user_id,
                                             Person.merged_into.is_(None))) is not None:
            continue
        user = db.get(User, m.user_id)
        card = profiles.get_profile(db, m.user_id)
        row = {"project_id": m.project_id, "user_id": m.user_id, "login": user.name if user else str(m.user_id)}
        if card is None or not (card.first_name and card.last_name):
            out.append({**row, "kind": "no_card"})
            continue
        name = f"{card.first_name} {card.last_name}"
        twin = next((h.person for n in (name, f"{card.last_name} {card.first_name}")
                     if (h := people.resolve(db, m.project_id, n, allow_first_name=False)) and h.person.user_id is None), None)
        out.append({**row, "name": name, "kind": "link" if twin is not None else "create"})
    return out


def apply(db, steps: list[dict]) -> int:
    from app.app.services import people

    done = 0
    for s in steps:
        if s["kind"] == "no_card":
            continue
        try:
            if people.member_person(db, s["project_id"], s["user_id"]) is not None:
                done += 1
        except people.PeopleError as exc:
            print(f"  пр. {s['project_id']}: {s['login']} — {exc}")
    db.commit()
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="применить (иначе только план)")
    ap.add_argument("--skip-project", type=int, action="append", default=[], help="не трогать проект (можно несколько раз)")
    args = ap.parse_args(argv)

    import app.app.models  # noqa: F401
    from app.app.config import get_settings
    from app.app.db import SessionLocal

    db = SessionLocal()
    try:
        steps = plan(db, set(args.skip_project))
        title = {"link": "привязать учётку к человеку с тем же именем", "create": "новый человек по карточке",
                 "no_card": "нужна карточка (фамилия, имя, компания)"}
        for s in steps:
            print(f"пр. {s['project_id']}: {s['login']}{' — ' + s['name'] if s.get('name') else ''} — {title[s['kind']]}")
        ready = [s for s in steps if s["kind"] != "no_card"]
        print(f"участников без человека: {len(steps)}; можно сейчас: {len(ready)}; нужна карточка: {len(steps) - len(ready)}")
        if not args.apply or not ready:
            print("база не менялась; применить — с --apply")
            return 0
        db_path = get_settings().data_dir / "app.db"
        backup = db_path.parent / "backups" / f"app.db.bak-link-members-{datetime.now():%Y%m%d-%H%M%S}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_path, backup)
        print("копия БД:", backup)
        print("готово:", apply(db, ready))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
