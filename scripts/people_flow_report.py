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
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "people"  # вне git: имена людей и компаний
SIDES = ("executor", "customer")
SIDE_RU = {"executor": "исполнитель", "customer": "заказчик"}


def company_key(name: str | None) -> str:
    """Ключ одной компании под разными написаниями — тот же, что у справочника (``companies.loose_key``)."""
    from app.app.services.companies import loose_key

    return loose_key(name)


def collect(db, settings, project_id: int | None = None) -> dict:
    """Расхождения по проектам и по порталу. Чистая выборка из БД, без записи."""
    from sqlalchemy import select

    from app.app.models.company import Company
    from app.app.models.meeting import STATUS_ENDED, Meeting, MeetingParticipant
    from app.app.models.person import Person, ProjectParty, UnknownActor
    from app.app.models.project import Project
    from app.app.models.project_member import ProjectMember
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile
    from app.app.models.user import User
    from app.app.models.user_profile import UserProfile
    from app.app.services import memory_platform

    q = select(Project).where(Project.is_active.is_(True)).order_by(Project.id)
    if project_id is not None:
        q = q.where(Project.id == project_id)
    projects = db.scalars(q).all()
    companies = db.scalars(select(Company)).all()
    directory = {company_key(c.name): c for c in companies if c.active}

    rows = []
    for project in projects:
        pid = project.id
        parties = db.scalars(select(ProjectParty).where(ProjectParty.project_id == pid)).all()
        party_side = {company_key(p.company_name): p.side for p in parties}
        people = db.scalars(select(Person).where(Person.project_id == pid, Person.merged_into.is_(None),
                                                 Person.is_active.is_(True))).all()
        by_user = {p.user_id for p in people if p.user_id}
        members = db.scalars(select(ProjectMember).where(ProjectMember.project_id == pid)).all()
        ended = db.scalars(select(Meeting.id).where(Meeting.project_id == pid, Meeting.status == STATUS_ENDED)).all()
        guests: dict[tuple[int, str], MeetingParticipant] = {}
        if ended:
            for part in db.scalars(select(MeetingParticipant).where(MeetingParticipant.meeting_id.in_(ended))).all():
                if part.identity.startswith("skkkk-"):
                    continue  # рекордеры и служебные участники
                key = (part.meeting_id, part.identity)
                if key not in guests or (part.person_id and not guests[key].person_id):
                    guests[key] = part
        unknown_open = db.scalars(select(UnknownActor).where(UnknownActor.project_id == pid, UnknownActor.status == "open")).all()

        def name_of(p: Person) -> str:
            return p.display_name

        no_company = [name_of(p) for p in people if not (p.company_name or "").strip()]
        side_unknown = [name_of(p) for p in people if p.side not in SIDES]
        side_conflict = [f"{name_of(p)}: {p.company_name} — у человека {SIDE_RU[p.side]}, у компании в проекте "
                         f"{SIDE_RU.get(party_side[company_key(p.company_name)], party_side[company_key(p.company_name)])}"
                         for p in people if p.side in SIDES and (p.company_name or "").strip()
                         and company_key(p.company_name) in party_side and party_side[company_key(p.company_name)] != p.side]
        not_in_parties = sorted({p.company_name for p in people if (p.company_name or "").strip()
                                 and company_key(p.company_name) not in party_side})
        rows.append({
            "id": pid,
            "name": project.name,
            "platform": memory_platform.enabled(settings, project),
            "internal": bool(project.settings().get("internal_project")),
            "parties": [{"company": p.company_name, "side": p.side, "in_directory": company_key(p.company_name) in directory,
                         "company_id": p.company_id} for p in parties],
            "has_customer": any(p.side == "customer" for p in parties),
            "has_executor": any(p.side == "executor" for p in parties),
            "parties_without_company_id": [p.company_name for p in parties if p.company_id is None],
            "people": len(people),
            "people_with_account": len(by_user),
            "no_company": no_company,
            "side_unknown": side_unknown,
            "side_conflict": side_conflict,
            "company_not_in_parties": not_in_parties,
            "members_without_person": [m.user_id for m in members if m.user_id not in by_user],
            "meeting_participants": len(guests),
            "meeting_participants_without_person": sorted({g.display_name for g in guests.values() if not g.person_id}),
            "meeting_participants_without_person_n": sum(1 for g in guests.values() if not g.person_id),
            "unknown_open": len(unknown_open),
        })

    # одна компания под разными написаниями: справочник, стороны проектов, люди
    spellings: dict[str, set[str]] = defaultdict(set)
    for c in companies:
        spellings[company_key(c.name)].add(c.name)
    for name in db.scalars(select(ProjectParty.company_name)).all():
        spellings[company_key(name)].add(name)
    for name in db.scalars(select(Person.company_name).where(Person.company_name.isnot(None))).all():
        if name.strip():
            spellings[company_key(name)].add(name)
    variants = sorted((sorted(v) for k, v in spellings.items() if k and len(v) > 1), key=lambda v: v[0].casefold())

    users = db.scalars(select(User)).all()
    cards = {c.user_id: c for c in db.scalars(select(UserProfile)).all()}
    no_card = [u.name for u in users if u.id not in cards or not (cards[u.id].last_name and cards[u.id].first_name)]
    card_no_company = [u.name for u in users if u.id in cards and cards[u.id].company_id is None]

    voices = db.scalars(select(SpeakerProfile)).all()
    confirmed = set(db.scalars(select(ArtifactSpeakerLink.profile_id).where(ArtifactSpeakerLink.status == "confirmed",
                                                                           ArtifactSpeakerLink.profile_id.isnot(None))).all())
    ownerless = [v for v in voices if v.user_id is None and v.person_id is None]
    portal = {
        "companies": len([c for c in companies if c.active]),
        "company_variants": variants,
        "parties_outside_directory": sorted({p["company"] for r in rows for p in r["parties"] if not p["in_directory"]}),
        "users": len(users),
        "users_without_card": no_card,
        "cards_without_company": card_no_company,
        "voices": len(voices),
        "voices_with_account": sum(1 for v in voices if v.user_id),
        "voices_with_person": sum(1 for v in voices if v.person_id),
        "voices_without_owner": len(ownerless),
        "voices_confirmed_without_owner": sorted(v.display_name for v in ownerless if v.id in confirmed),
    }
    return {"built": datetime.now().strftime("%Y-%m-%d %H:%M"), "projects": rows, "portal": portal}


def totals(report: dict) -> dict[str, int]:
    """Числа сводки (без имён) — для консоли, табло и сверки с планом."""
    rows, portal = report["projects"], report["portal"]
    return {
        "проектов": len(rows),
        "с платформой": sum(1 for r in rows if r["platform"]),
        "без сторон": sum(1 for r in rows if not r["parties"]),
        "внутренних (только исполнитель)": sum(1 for r in rows if r["internal"]),
        "без заказчика (не внутренние)": sum(1 for r in rows if r["parties"] and not r["has_customer"] and not r["internal"]),
        "сторон без company_id": sum(len(r["parties_without_company_id"]) for r in rows),
        "компаний сторон вне справочника": len(portal["parties_outside_directory"]),
        "компаний в разных написаниях": len(portal["company_variants"]),
        "людей": sum(r["people"] for r in rows),
        "людей без компании": sum(len(r["no_company"]) for r in rows),
        "людей без стороны": sum(len(r["side_unknown"]) for r in rows),
        "людей со стороной против компании": sum(len(r["side_conflict"]) for r in rows),
        "людей с компанией не из сторон проекта": sum(len(r["company_not_in_parties"]) for r in rows),
        "участников проекта без человека": sum(len(r["members_without_person"]) for r in rows),
        "участников завершённых встреч": sum(r["meeting_participants"] for r in rows),
        "из них без человека": sum(r["meeting_participants_without_person_n"] for r in rows),
        "открытых «Кто это?»": sum(r["unknown_open"] for r in rows),
        "учёток": portal["users"],
        "учёток без карточки": len(portal["users_without_card"]),
        "голосов": portal["voices"],
        "голосов без владельца": portal["voices_without_owner"],
        "голосов с подтверждёнными созвонами без владельца": len(portal["voices_confirmed_without_owner"]),
    }


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
