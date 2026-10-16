"""Участники прошлых встреч → люди проектов (docs/plans/PEOPLE-FLOW-PLAN.md, ЛГ5).

    venv\\Scripts\\python.exe scripts\\link_meeting_people.py                       # план (база не меняется)
    venv\\Scripts\\python.exe scripts\\link_meeting_people.py --skip-project 35     # без этих проектов
    venv\\Scripts\\python.exe scripts\\link_meeting_people.py --enable-platform --apply  # применить; копия БД в data/backups/

Для каждой завершённой встречи активного проекта — то же, что делается теперь после каждой встречи
(``people.link_meeting_participants``): сотрудник — человек по учётке, гость — по имени, иначе новый человек проекта с
согласием на голос (Р7; компания — указанная при входе, иначе единственный заказчик проекта); «Кто это?» — только для
неоднозначного имени. ``--enable-platform`` включает платформу у проектов, где она выключена, а стороны уже заданы (Р4):
без неё связывание не работает. План считается прогоном с откатом. Имена — только в консоль (не в git).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run(db, settings, *, skip: set[int], enable_platform: bool) -> tuple[Counter, list[str]]:
    """Связать участников (без commit): итог по видам и список включённых платформ."""
    from sqlalchemy import select

    from app.app.models.meeting import STATUS_ENDED, Meeting
    from app.app.models.person import ProjectParty
    from app.app.models.project import Project
    from app.app.services import memory_platform, people

    total: Counter = Counter()
    enabled: list[str] = []
    for project in db.scalars(select(Project).where(Project.is_active.is_(True)).order_by(Project.id)).all():
        if project.id in skip:
            continue
        if not memory_platform.enabled(settings, project):
            has_parties = db.scalar(select(ProjectParty.id).where(ProjectParty.project_id == project.id)) is not None
            if not (enable_platform and has_parties):
                continue
            project.set_settings({**project.settings(), "memory_platform": True})
            enabled.append(f"пр. {project.id}")
        for meeting in db.scalars(select(Meeting).where(Meeting.project_id == project.id, Meeting.status == STATUS_ENDED)).all():
            out = people.link_meeting_participants(db, meeting)
            total.update(out)
    db.flush()
    return total, enabled


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="применить (иначе только план)")
    ap.add_argument("--enable-platform", action="store_true", help="включить платформу у проектов со сторонами (Р4)")
    ap.add_argument("--skip-project", type=int, action="append", default=[], help="не трогать проект (можно несколько раз)")
    args = ap.parse_args(argv)

    import app.app.models  # noqa: F401
    from app.app.config import get_settings
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        if args.apply:
            db_path = settings.data_dir / "app.db"
            backup = db_path.parent / "backups" / f"app.db.bak-link-meetings-{datetime.now():%Y%m%d-%H%M%S}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db_path, backup)
            print("копия БД:", backup)
        total, enabled = run(db, settings, skip=set(args.skip_project), enable_platform=args.enable_platform)
        print("платформа включена:", ", ".join(enabled) or "нигде")
        print(f"участников связано: {total['linked']}; из них новых людей: {total['created']}; вопросов «Кто это?»: {total['unknown']}")
        if args.apply:
            db.commit()
            print("готово")
        else:
            db.rollback()
            print("база не менялась; применить — с --apply")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
