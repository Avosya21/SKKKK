"""Отчёт расхождений «компания → сторона → человек → учётка → голос» (docs/plans/PEOPLE-FLOW-PLAN.md, ЛГ0).

    venv\\Scripts\\python.exe scripts\\people_flow_report.py              # сводка в консоль, подробности в data/people/
    venv\\Scripts\\python.exe scripts\\people_flow_report.py --project 44 # один проект

Ничего не меняет. В консоль — только числа; имена людей и компаний — в отчёте ``data/people/people_flow_report.md`` и
``.json`` (вне git). По каждому активному проекту: стороны и их связь со справочником компаний, люди без компании, без
стороны или со стороной против своей компании, участники проекта без человека реестра, участники завершённых встреч без
человека, открытые «Кто это?». По порталу: компании справочника, одна компания в разных написаниях, учётки без
карточки, голоса без владельца. Те же числа потом показывает табло (ЛГ8).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "people"  # вне git: имена людей и компаний
from app.app.services.people_flow import SIDE_RU, collect, company_key, totals  # noqa: E402,F401 — расчёт в сервисе (табло)


def render_md(report: dict) -> str:
    """Подробный отчёт с именами — только в data/people/."""
    lines = [f"# Люди, компании, учётки и голоса — расхождения ({report['built']})", "",
             "План: docs/plans/PEOPLE-FLOW-PLAN.md (ЛГ0). Файл содержит имена — не переносить в git.", "", "## Сводка", ""]
    lines += [f"- {k}: {v}" for k, v in totals(report).items()]
    portal = report["portal"]

    def block(title: str, items: list) -> None:
        if items:
            lines.append(f"- {title}: " + "; ".join(str(x) for x in items))

    lines += ["", "## Портал", ""]
    block("одна компания в разных написаниях", [" / ".join(v) for v in portal["company_variants"]])
    block("компании сторон проектов вне справочника", portal["parties_outside_directory"])
    block("учётки без карточки", portal["users_without_card"])
    block("карточки без компании", portal["cards_without_company"])
    block("голоса с подтверждёнными созвонами без владельца", portal["voices_confirmed_without_owner"])
    lines += ["", "## Проекты", ""]
    for r in report["projects"]:
        parties = ", ".join(f"{p['company']} — {SIDE_RU.get(p['side'], p['side'])}{'' if p['in_directory'] else ' (нет в справочнике)'}"
                            for p in r["parties"]) or "нет"
        lines += [f"### пр. {r['id']} · {r['name']}", "",
                  f"- платформа: {'включена' if r['platform'] else 'выключена'}; стороны: {parties}",
                  f"- людей {r['people']}, с учёткой {r['people_with_account']}; участников встреч {r['meeting_participants']}, "
                  f"без человека {r['meeting_participants_without_person_n']}; открытых «Кто это?» {r['unknown_open']}"]
        block("без компании", r["no_company"])
        block("без стороны", r["side_unknown"])
        block("сторона против компании", r["side_conflict"])
        block("компания не из сторон проекта", r["company_not_in_parties"])
        block("участники проекта без человека (id учётки)", r["members_without_person"])
        block("участники встреч без человека", r["meeting_participants_without_person"])
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", type=int, help="только этот проект")
    ap.add_argument("--out", type=Path, default=OUT, help="папка отчёта (по умолчанию data/people/)")
    args = ap.parse_args(argv)

    import app.app.models  # noqa: F401
    from app.app.config import get_settings
    from app.app.db import SessionLocal

    db = SessionLocal()
    try:
        report = collect(db, get_settings(), args.project)
    finally:
        db.close()
    for key, value in totals(report).items():
        print(f"{key}: {value}")
    args.out.mkdir(parents=True, exist_ok=True)
    stem = "people_flow_report" if args.project is None else f"people_flow_report_{args.project}"
    (args.out / f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (args.out / f"{stem}.md").write_text(render_md(report), encoding="utf-8")
    print("подробно:", args.out / f"{stem}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
