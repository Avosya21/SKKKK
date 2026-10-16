"""Сквозной путь «компания → сторона → человек → учётка → голос»: расхождения по проектам и порталу
(docs/plans/PEOPLE-FLOW-PLAN.md, ЛГ0 и ЛГ8). Только чтение. Считает отчёт ``scripts/people_flow_report.py`` и блок
«Люди и голоса» на табло ``/admin/memory`` (цели — §6 плана).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

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
            "new_cards": sum(1 for p in people if p.needs_review),
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


def _words(db, project_ids: set[int]) -> tuple[int, int]:
    """Слова людей с карточкой: реплики (``speech_acts``) проектов, кроме авторов-документов и неподписанных меток."""
    from sqlalchemy import select

    from app.app.models.speech_act import SpeechAct

    total = linked = 0
    if not project_ids:
        return 0, 0
    for author, person_id in db.execute(select(SpeechAct.author, SpeechAct.person_id)
                                        .where(SpeechAct.project_id.in_(project_ids))).all():
        text = (author or "").strip()
        if not text or text.startswith("Документ «") or text.startswith("speaker_"):
            continue
        total += 1
        linked += person_id is not None
    return linked, total


def _voices(db, project_ids: set[int]) -> tuple[int, int]:
    """Голоса, подтверждённые в созвонах проектов с платформой: сколько привязаны к человеку — владелец (учётка или человек
    проекта) или человек с именем голоса есть в каждом таком проекте (сотрудник без учётки: свой человек в каждом проекте)."""
    from collections import defaultdict as _dd

    from sqlalchemy import select

    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile
    from app.app.services import people

    heard: dict[int, set[int]] = _dd(set)
    for profile_id, project_id in db.execute(select(ArtifactSpeakerLink.profile_id, ArtifactSpeakerLink.project_id)
                                             .where(ArtifactSpeakerLink.status == "confirmed",
                                                    ArtifactSpeakerLink.profile_id.isnot(None))).all():
        if project_id in project_ids:
            heard[profile_id].add(project_id)
    linked = 0
    for profile_id, projects in heard.items():
        voice = db.get(SpeakerProfile, profile_id)
        if voice is None:
            continue
        if voice.user_id or voice.person_id or all(people.resolve(db, pid, voice.display_name, allow_first_name=False)
                                                   for pid in projects):
            linked += 1
    return linked, len(heard)


def board(db, report: dict) -> list[dict]:
    """Плитки табло: значение, цель и выполнена ли она. Проекты — с включённой платформой (остальные не связываются)."""
    rows = [r for r in report["projects"] if r["platform"]]

    def share(num: int, den: int) -> float | None:
        return round(num / den, 4) if den else None

    people = sum(r["people"] for r in rows)
    sided = people - sum(len(set(r["no_company"]) | set(r["side_unknown"])) for r in rows)
    parties = sum(len(r["parties"]) for r in rows)
    parties_ok = parties - sum(len(r["parties_without_company_id"]) for r in rows)
    members = sum(len(r["members_without_person"]) for r in rows)
    meeting = sum(r["meeting_participants"] for r in rows)
    meeting_ok = meeting - sum(r["meeting_participants_without_person_n"] for r in rows)
    voices_ok, voices = _voices(db, {r["id"] for r in rows})
    words_ok, words = _words(db, {r["id"] for r in rows})
    tiles = [
        ("parties", "Стороны проектов с компанией справочника", share(parties_ok, parties), 1.0),
        ("people", "Люди: компания и сторона известны", share(sided, people), 1.0),
        ("conflict", "Люди со стороной против компании", sum(len(r["side_conflict"]) for r in rows), 0),
        ("voices", "Голоса из созвонов: привязаны к человеку", share(voices_ok, voices), 0.9),
        ("meetings", "Участники встреч: человек проекта", share(meeting_ok, meeting), 0.95),
        ("words", "Слова людей с карточкой", share(words_ok, words), 0.95),
        ("members", "Участники проекта без человека", members, 0),
        ("review", "Новые карточки на подтверждении", sum(r.get("new_cards", 0) for r in rows), None),
        ("unknown", "Открытые «Кто это?»", sum(r["unknown_open"] for r in rows), None),
    ]
    out = []
    for key, title, value, target in tiles:
        if target is None:
            ok = None
        elif isinstance(target, int) and not isinstance(target, bool) and target == 0 and isinstance(value, int):
            ok = value == 0
        else:
            ok = value is not None and value >= target
        out.append({"key": key, "title": title, "value": value, "target": target, "ok": ok})
    return out
