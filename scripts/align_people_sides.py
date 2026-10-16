"""Привести людей проектов к правилу «сторона — от компании в проекте» (docs/plans/PEOPLE-FLOW-PLAN.md, ЛГ2, решение Р1).

    venv\\Scripts\\python.exe scripts\\align_people_sides.py                    # план (база не меняется)
    venv\\Scripts\\python.exe scripts\\align_people_sides.py --skip-project 35  # без этих проектов
    venv\\Scripts\\python.exe scripts\\align_people_sides.py --apply            # применить; копия БД в data/backups/

Что делает (только там, где решение однозначно):
- у человека с компанией из сторон проекта — сторона и написание компании как у стороны проекта (если сторона та же,
  меняется только пометка источника `project_party`; в плане такие не печатаются);
- у человека без компании, но с известной стороной, — компания этой стороны, если она в проекте одна;
- у человека с компанией не из сторон проекта и известной стороной — компания входит в стороны проекта.
Остальные (нет ни компании, ни стороны; компания не в сторонах при неизвестной стороне) печатаются как «нужно решение».
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

SIDE_RU = {"executor": "исполнитель", "customer": "заказчик", "unknown": "не определена"}


def plan(db, skip: set[int] | None = None) -> list[dict]:
    """Шаги по каждому человеку: ``kind`` — side_from_party | company_by_side | add_party | decide."""
    from sqlalchemy import select

    from app.app.models.person import ProjectParty
    from app.app.models.project import Project
    from app.app.services import people

    out: list[dict] = []
    for project in db.scalars(select(Project).where(Project.is_active.is_(True)).order_by(Project.id)).all():
        if skip and project.id in skip:
            continue
        parties = db.scalars(select(ProjectParty).where(ProjectParty.project_id == project.id)).all()
        for person in people.list_people(db, project.id):
            base = {"project_id": project.id, "person_id": person.id, "name": person.display_name,
                    "company": person.company_name or "", "side": person.side}
            company = (person.company_name or "").strip()
            if company:
                party = people.find_party(db, project.id, company)
                if party is not None:
                    if (person.side, person.company_name) != (party.side, party.company_name) or person.side_source != "project_party":
                        real = (person.side, person.company_name) != (party.side, party.company_name)
                        out.append({**base, "kind": "side_from_party" if real else "source_only",
                                    "to_company": party.company_name, "to_side": party.side})
                elif person.side in ("executor", "customer"):
                    out.append({**base, "kind": "add_party", "to_company": company, "to_side": person.side})
                else:
                    out.append({**base, "kind": "decide", "why": "компании нет в сторонах проекта, сторона неизвестна"})
                continue
            same = [p for p in parties if p.side == person.side]
            if person.side in ("executor", "customer") and len(same) == 1:
                out.append({**base, "kind": "company_by_side", "to_company": same[0].company_name, "to_side": person.side})
            elif person.side not in ("executor", "customer"):
                out.append({**base, "kind": "decide", "why": "нет ни компании, ни стороны"})
            else:
                out.append({**base, "kind": "decide", "why": f"на стороне «{SIDE_RU[person.side]}» в проекте {len(same)} компаний"})
    return out


def apply(db, steps: list[dict]) -> int:
    from app.app.services import people

    done = 0
    for s in steps:
        person = people.get(db, s["project_id"], s["person_id"])
        if s["kind"] == "company_by_side":
            people.update_person(db, s["project_id"], person.id, company_name=s["to_company"])
        elif s["kind"] in ("side_from_party", "source_only", "add_party"):
            people.align_to_company(db, s["project_id"], person)
        else:
            continue
        done += 1
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
        title = {"side_from_party": "сторона от компании", "company_by_side": "компания единственной стороны",
                 "add_party": "компания входит в стороны проекта", "decide": "нужно решение"}
        for s in steps:
            if s["kind"] == "source_only":
                continue  # сторона та же, меняется только пометка «откуда сторона»
            to = f" → {s['to_company']}, {SIDE_RU[s['to_side']]}" if s.get("to_company") else f" ({s['why']})"
            print(f"пр. {s['project_id']}: {s['name']} [{s['company'] or 'без компании'}, {SIDE_RU.get(s['side'], s['side'])}] — "
                  f"{title[s['kind']]}{to}")
        auto = [s for s in steps if s["kind"] != "decide"]
        quiet = sum(1 for s in steps if s["kind"] == "source_only")
        print(f"автоматически: {len(auto)} (из них {quiet} — сторона та же, меняется только пометка источника); "
              f"нужно решение: {len(steps) - len(auto)}")
        if not args.apply or not auto:
            print("база не менялась; применить — с --apply")
            return 0
        db_path = get_settings().data_dir / "app.db"
        backup = db_path.parent / "backups" / f"app.db.bak-align-sides-{datetime.now():%Y%m%d-%H%M%S}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_path, backup)
        print("копия БД:", backup)
        print("готово:", apply(db, auto))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
