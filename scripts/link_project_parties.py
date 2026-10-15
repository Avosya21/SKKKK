"""Свести стороны проектов со справочником компаний (docs/plans/PEOPLE-FLOW-PLAN.md, ЛГ1).

    venv\\Scripts\\python.exe scripts\\link_project_parties.py           # план: что с чем свяжется (база не меняется)
    venv\\Scripts\\python.exe scripts\\link_project_parties.py --apply   # применить; перед этим копия БД в data/backups/

Сторона проекта, записанная до ЛГ1 только текстом (без ``company_id``), связывается с компанией справочника: то же
название без учёта регистра, кавычек и формы собственности — с существующей; нет такой — компания добавляется в
справочник со стороной по роли в проекте (решение Р2). Написание стороны и компании людей проекта приводится к
справочнику. Повторный запуск ничего не меняет.
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

SIDE_RU = {"executor": "исполнитель", "customer": "заказчик"}


def plan(db) -> list[dict]:
    """Стороны без ``company_id`` и что с ними будет; без записи."""
    from sqlalchemy import select

    from app.app.models.person import ProjectParty
    from app.app.services.companies import canonical_company, sync_companies

    sync_companies(db)  # справочник из файла-источника
    out = []
    for row in db.scalars(select(ProjectParty).where(ProjectParty.company_id.is_(None)).order_by(ProjectParty.project_id)).all():
        company = canonical_company(db, row.company_name)
        out.append({"project_id": row.project_id, "party": row.company_name, "side": row.side,
                    "company": company.name if company is not None else None,
                    "company_active": bool(company is not None and company.active)})
    return out


def apply(db, steps: list[dict]) -> int:
    """Связать стороны через ``people.set_party`` и привести компании людей проекта к справочнику."""
    from app.app.services import people

    projects = set()
    for s in steps:
        people.set_party(db, s["project_id"], s["party"], s["side"])
        projects.add(s["project_id"])
    renamed = sum(people.normalize_project_companies(db, pid) for pid in sorted(projects))
    db.commit()
    return renamed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="применить (иначе только план)")
    args = ap.parse_args(argv)

    import app.app.models  # noqa: F401
    from app.app.config import get_settings
    from app.app.db import SessionLocal

    db = SessionLocal()
    try:
        steps = plan(db)
        for s in steps:
            where = (f"связать с «{s['company']}»" + ("" if s["company_active"] else " (скрыта — вернётся в справочник)")
                     if s["company"] else f"добавить в справочник как {SIDE_RU.get(s['side'], s['side'])}")
            print(f"пр. {s['project_id']}: «{s['party']}» ({SIDE_RU.get(s['side'], s['side'])}) — {where}")
        print(f"сторон без компании справочника: {len(steps)}; новых компаний: {len({s['party'].casefold() for s in steps if not s['company']})}")
        if not args.apply or not steps:
            print("база не менялась; применить — с --apply")
            return 0
        db_path = get_settings().data_dir / "app.db"
        backup = db_path.parent / "backups" / f"app.db.bak-link-parties-{datetime.now():%Y%m%d-%H%M%S}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_path, backup)
        print("копия БД:", backup)
        renamed = apply(db, steps)
        print(f"готово: сторон связано {len(steps)}, у людей приведено написаний компаний {renamed}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
