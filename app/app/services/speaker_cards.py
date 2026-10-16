"""Карточка голоса на странице «Голоса» (/report-speaker-match): имя, компания, сторона и должность сразу.

Подтверждение голоса заводит или правит человека в реестре проекта («Люди и стороны»), чтобы менеджеру не приходилось
второй раз заполнять его на странице проекта. Компания и сторона обязательны; должность — по желанию. Если система уже
догадывается, кто это (реестр проекта, карточка сотрудника по голосу или по имени), поля заполняются, но голос принимается
только по кнопке человека — и узнанные автоматически тоже (``needs_human`` в match, см. ``speaker_auto``).

Человек без учётки получает отметку «согласие на голос получено» с основанием «договор» (решение 2026-10-15), кроме тех,
у кого согласие отзывали: отзыв — только консоль (Д4), повторно здесь не ставится.

Сведения о людях других проектов не используются (ПФ8): подсказка — только из реестра этого проекта и карточек
сотрудников исполнителя.

«Кто это» выбирается из списка (решение 2026-10-15): похожие голоса базы с процентом сходства, люди проекта, сотрудники;
«Новый человек» — только если в списке его нет. Выбор привязывает голос к той самой записи (``profile_id`` / ``person_id``),
а не к совпадению написания. Другое имя на подтверждённой карточке — перепривязка; переименовать голос во всех созвонах —
только явно (``rename``).
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact
from app.app.models.person import SIDE_CUSTOMER, SIDE_EXECUTOR, PeopleLog, Person, UnknownActor
from app.app.services import people

CARD_SIDES = (SIDE_EXECUTOR, SIDE_CUSTOMER)
CANDIDATES_PER_CARD = 5
# Ниже — не тот же человек, а шум: на проекте 40 у незнакомых голосов лучшие совпадения 45–50 %, у узнанных 78–91 %.
CANDIDATE_MIN_COSINE = 0.55
CONSENT_BASIS = "договор (подтверждение голоса на странице «Голоса»)"
SOURCE_REGISTRY = "Люди и стороны"
SOURCE_STAFF = "карточка сотрудника"


def _staff(db: Session) -> list[dict[str, Any]]:
    """Сотрудники исполнителя с заполненной карточкой: имя (оба порядка), компания, должность, учётка."""
    from app.app.models.company import Company
    from app.app.models.user_profile import UserProfile

    rows = db.execute(
        select(UserProfile.user_id, UserProfile.last_name, UserProfile.first_name, UserProfile.job_title, Company.name, Company.side)
        .join(Company, Company.id == UserProfile.company_id)
        .where(Company.side == SIDE_EXECUTOR)
    ).all()
    out = []
    for user_id, last, first, job, company, side in rows:
        name = " ".join(p for p in (first, last) if p).strip()
        if not name:
            continue
        keys = sorted({people.alias_key(name), people.alias_key(f"{last} {first}")} - {""})
        out.append({"user_id": user_id, "name": name, "keys": keys, "company": company, "side": side, "job_title": job or ""})
    return out


def _person_row(p: Person, keys: list[str]) -> dict[str, Any]:
    return {"id": p.id, "name": p.display_name, "keys": keys, "company": p.company_name or "",
            "side": p.side if p.side in CARD_SIDES else "", "job_title": p.job_title or "", "account": p.user_id is not None}


def _company_side(db: Session, project_id: int, company: str | None) -> str:
    """Сторона компании: в этом проекте (``project_parties``), иначе по общему списку компаний; не знаем — пусто."""
    from app.app.services.companies import canonical_company

    side = people.party_side(db, project_id, company)
    if side in CARD_SIDES:
        return side
    row = canonical_company(db, company)
    return row.side if row is not None and row.side in CARD_SIDES else ""


def _match_payload(artifact: Artifact) -> dict:
    from app.app.services.speaker_profiles import match_json_path

    path = match_json_path(artifact.project_id, artifact.artifact_id)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def suggest(db: Session, project_id: int, name: str | None, *, profile_id: int | None = None,
            staff: list[dict] | None = None) -> dict[str, Any] | None:
    """Кто это по имени (или по голосу): человек проекта, иначе сотрудник по голосу или по имени. Ничего — None."""
    from app.app.models.speaker_profile import SpeakerProfile

    match = people.resolve(db, project_id, name) if (name or "").strip() else None
    if match is not None:
        p = match.person
        row = _person_row(p, [])
        if not row["side"] and p.company_name:
            row["side"] = _company_side(db, project_id, p.company_name)
        return {**row, "source": SOURCE_REGISTRY}
    staff = _staff(db) if staff is None else staff
    prof = db.get(SpeakerProfile, profile_id) if profile_id else None
    user_id = prof.user_id if prof is not None else None
    key = people.alias_key(name)
    hit = next((s for s in staff if (user_id and s["user_id"] == user_id) or (key and key in s["keys"])), None)
    if hit is None:
        return None
    side = _company_side(db, project_id, hit["company"]) or hit["side"]
    return {"name": hit["name"], "company": hit["company"], "side": side, "job_title": hit["job_title"],
            "account": True, "source": SOURCE_STAFF}


def page_context(db: Session, artifact: Artifact) -> dict[str, Any]:
    """Данные для полей карточек: люди проекта, сотрудники, компании, стороны компаний и подсказка на каждую карточку."""
    from app.app.models.person import PersonAlias, ProjectParty
    from app.app.services.companies import active_companies

    project_id = artifact.project_id
    persons = people.list_people(db, project_id)
    alias_rows = db.scalars(select(PersonAlias).where(PersonAlias.project_id == project_id)).all()
    keys: dict[int, list[str]] = {}
    for a in alias_rows:
        keys.setdefault(a.person_id, []).append(a.alias_key)
    staff = _staff(db)
    companies: dict[str, str] = {}
    sides: dict[str, str] = {}
    for c in active_companies(db):
        companies.setdefault(people.alias_key(c.name), c.name)
        if c.side in CARD_SIDES:
            sides[people.alias_key(c.name)] = c.side
    for party in db.scalars(select(ProjectParty).where(ProjectParty.project_id == project_id)).all():
        companies.setdefault(party.company_key, party.company_name)
        if party.side in CARD_SIDES:
            sides[party.company_key] = party.side  # сторона в проекте главнее общего списка (Д1)
    for p in persons:
        if p.company_name:
            companies.setdefault(people.alias_key(p.company_name), p.company_name)
    payload = _match_payload(artifact)
    merge_map = payload.get("merge_map") or {}
    aliases = payload.get("voice_aliases") or {}
    cards: dict[str, Any] = {}
    for label, row in (payload.get("matches") or {}).items():
        if not isinstance(row, dict) or merge_map.get(label, label) != label:
            continue
        status = row.get("status") or "new"
        name = (row.get("profile_name") or "").strip() if status == "confirmed" else ""
        hint = suggest(db, project_id, name, profile_id=row.get("profile_id") if status == "confirmed" else None, staff=staff) if name else None
        current = {"profile_id": row.get("profile_id"), "name": name} if status == "confirmed" and row.get("profile_id") else None
        cards[label] = {"status": status, "needs_human": bool(row.get("needs_human")), "suggestion": hint, "current": current,
                        "candidates": _candidates(db, project_id, (payload.get("profile_candidates") or {}).get(label) or [],
                                                  aliases, staff)}
    for label, target in merge_map.items():
        if label != target and target in cards and label in (payload.get("matches") or {}):
            cards[label] = cards[target]  # склеенный по голосу (можно разделить) — варианты основного голоса
    rows = []
    for p in persons:
        row = _person_row(p, sorted(set(keys.get(p.id, [])) | {people.alias_key(p.display_name)}))
        row["profile_id"] = voice_of(db, p)
        rows.append(row)
    return {
        "artifact_id": artifact.artifact_id,
        "people": rows,
        "staff": [{k: s[k] for k in ("name", "keys", "company", "side", "job_title")} for s in staff],
        "companies": sorted(companies.values(), key=str.casefold),
        "company_sides": sides,
        "cards": cards,
    }


def _candidates(db: Session, project_id: int, raw: list, aliases: dict, staff: list[dict]) -> list[dict[str, Any]]:
    """Похожие голоса карточки (``profile_candidates`` match): имя (служебное — через ``voice_aliases``), %, кто это."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cand in raw:
        if not isinstance(cand, dict) or not cand.get("profile_id") or cand.get("cosine") is None:
            continue
        if float(cand["cosine"]) < CANDIDATE_MIN_COSINE:
            continue
        name = str(aliases.get(cand.get("display_name")) or cand.get("display_name") or "").strip()
        key = people.alias_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"profile_id": int(cand["profile_id"]), "name": name, "pct": int(round(float(cand["cosine"]) * 100)),
                    "info": suggest(db, project_id, name, profile_id=int(cand["profile_id"]), staff=staff)})
        if len(out) >= CANDIDATES_PER_CARD:
            break
    return out


def voice_of(db: Session, person: Person) -> int | None:
    """Голос человека проекта: свой слепок проекта, общий голос его учётки, иначе общий голос с тем же именем."""
    from app.app.models.speaker_profile import SpeakerProfile
    from app.app.services.speaker_profiles import _visible_profile_by_name

    own = db.scalar(select(SpeakerProfile.id).where(SpeakerProfile.person_id == person.id).limit(1))
    if own is None and person.user_id is not None:
        own = db.scalar(select(SpeakerProfile.id).where(SpeakerProfile.user_id == person.user_id, SpeakerProfile.scope == "global").limit(1))
    if own is None:
        hit = _visible_profile_by_name(db, person.project_id, person.display_name)
        own = hit.id if hit is not None else None
    return own


def _consent_withdrawn(db: Session, person_id: int) -> bool:
    return db.scalar(select(PeopleLog.id).where(PeopleLog.person_id == person_id, PeopleLog.action == "voice_withdraw").limit(1)) is not None


def _person_by_name(db: Session, project_id: int, name: str, *, company: str, side: str, job_title: str | None,
                    by_user_id: int | None) -> Person:
    """Человек проекта по имени (или по написанию); сотрудник — из его карточки; иначе новый."""
    match = people.resolve(db, project_id, name)
    if match is None:
        hit = next((s for s in _staff(db) if people.alias_key(name) in s["keys"]), None)
        own = db.scalar(select(Person).where(Person.project_id == project_id, Person.user_id == hit["user_id"],
                                             Person.merged_into.is_(None))) if hit is not None else None
        if own is not None:
            person = own  # сотрудник уже в реестре под другим написанием — запомнить и это
            people.add_alias(db, project_id, person.id, name, source="voice", by_user_id=by_user_id)
        elif hit is not None:
            person = people.person_from_user(db, project_id, hit["user_id"], by_user_id=by_user_id)
        else:
            return people.create_person(db, project_id, name, company_name=company, side=side, side_source="human",
                                        job_title=job_title or None, by_user_id=by_user_id)
    else:
        person = match.person
        _alias(db, project_id, person, name, by_user_id)
    return person


def _alias(db: Session, project_id: int, person: Person, name: str, by_user_id: int | None) -> None:
    """Запомнить написание (имя голоса) у человека, если оно другое и не занято другим человеком."""
    if people.alias_key(name) and people.alias_key(name) != people.alias_key(person.display_name):
        try:
            people.add_alias(db, project_id, person.id, name, source="voice", by_user_id=by_user_id)
        except people.PeopleError:
            pass


def _apply_fields(db: Session, project_id: int, person: Person, *, company: str, side: str, job_title: str | None,
                  by_user_id: int | None) -> Person:
    changes: dict[str, Any] = {}
    if people.alias_key(company) != people.alias_key(person.company_name):
        changes["company_name"] = company
    if job_title and job_title != (person.job_title or ""):
        changes["job_title"] = job_title
    if changes:
        person = people.update_person(db, project_id, person.id, by_user_id=by_user_id, **changes)
    if person.side != side or person.side_source != "human":
        person = people.set_side(db, project_id, person.id, side, source="human", by_user_id=by_user_id, force=True)
    return person


def confirm_card(db: Session, artifact: Artifact, *, diar_label: str, name: str, company: str, side: str,
                 job_title: str | None = None, by_user_id: int | None = None, profile_id: int | None = None,
                 person_id: int | None = None, new_person: bool = False, rename: bool = False):
    """Подтвердить голос человеком: реестр проекта (человек, сторона компании, согласие) → запомнить голос под его именем.

    ``profile_id`` — выбран голос из базы; ``person_id`` — человек проекта; ``new_person`` — «новый человек» (имя не должно
    совпадать с уже известным); ``rename`` — исправить написание голоса этой карточки во всех созвонах."""
    from app.app.models.speaker_profile import SpeakerProfile
    from app.app.services.speaker_profiles import _visible_profile_by_name, enroll_speaker

    name, company = " ".join((name or "").split()), " ".join((company or "").split())
    job_title = " ".join((job_title or "").split()) or None
    if not name:
        raise people.PeopleError("Введите имя спикера")
    if not company:
        raise people.PeopleError("Укажите компанию спикера")
    if side not in CARD_SIDES:
        raise people.PeopleError("Укажите сторону: исполнитель или заказчик")
    project_id = artifact.project_id
    party = people.find_party(db, project_id, company)
    if party is not None and party.side != side:  # Р1: сторона — у компании в проекте
        raise people.PeopleError(f"«{party.company_name}» в этом проекте — {people.SIDE_RU.get(party.side, party.side)}. "
                                 "Выберите эту сторону или поменяйте её у компании в «Стороны компаний».")
    fields = {"company": company, "side": side, "job_title": job_title, "by_user_id": by_user_id}
    voice_name = name
    try:
        chosen = db.get(SpeakerProfile, profile_id) if profile_id else None
        if profile_id and (chosen is None or not (chosen.scope == "global" or chosen.project_id == project_id)):
            raise people.PeopleError("Выбранный голос не найден — обновите страницу")
        if rename:
            if chosen is None:
                raise people.PeopleError("Переименовать можно только голос этой карточки")
            old = people.resolve(db, project_id, chosen.display_name)
            if old is not None and people.alias_key(old.person.display_name) != people.alias_key(name):
                person = people.update_person(db, project_id, old.person.id, by_user_id=by_user_id, display_name=name)
            else:
                person = _person_by_name(db, project_id, name, **fields)
        elif chosen is not None:
            voice_name = chosen.display_name
            if person_id:
                person = people.get(db, project_id, person_id)
            else:
                hit = suggest(db, project_id, voice_name, profile_id=chosen.id)
                person = _person_by_name(db, project_id, (hit or {}).get("name") or voice_name, **fields)
            _alias(db, project_id, person, voice_name, by_user_id)  # имя голоса — написание этого человека
        elif person_id:
            person = people.get(db, project_id, person_id)
            voice_name = person.display_name
            profile_id = voice_of(db, person)
        elif new_person:
            taken = people.resolve(db, project_id, name, allow_first_name=False)
            if taken is not None:
                raise people.PeopleError(f"«{taken.person.display_name}» уже есть среди людей проекта — выберите его в списке.")
            twin = _visible_profile_by_name(db, project_id, name)
            if twin is not None:
                raise people.PeopleError(f"Голос «{twin.display_name}» уже есть в базе — выберите его в списке или уточните имя "
                                         "(например, фамилией).")
            person = _person_by_name(db, project_id, name, **fields)
        else:
            person = _person_by_name(db, project_id, name, **fields)
        if not rename and person_id is None and chosen is None:
            voice_name = person.display_name
        person = _apply_fields(db, project_id, person, **fields)
        if people.party_side(db, project_id, company) is None:
            company_id, company_name = people.normalize_company(db, company)
            people.set_party(db, project_id, company_name or company, side, company_id=company_id, by_user_id=by_user_id)
        if person.user_id is None and person.voice_consent_at is None:
            if _consent_withdrawn(db, person.id):
                raise people.PeopleError(f"У «{person.display_name}» согласие на запись голоса отозвано — голос не сохраняется.")
            people.give_voice_consent(db, project_id, person.id, by_user_id=by_user_id, basis=CONSENT_BASIS)
        for actor in db.scalars(select(UnknownActor).where(
                UnknownActor.project_id == project_id, UnknownActor.kind == "speaker_label", UnknownActor.status == "open",
                UnknownActor.label_key.in_({people.alias_key(name), people.alias_key(person.display_name)}))).all():
            people.resolve_unknown(db, project_id, actor.id, person_id=person.id, by_user_id=by_user_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    profile = enroll_speaker(db, project_id=project_id, artifact_id=artifact.artifact_id, diar_label=diar_label,
                             display_name=name if rename else voice_name, profile_id=profile_id, new_voice=new_person,
                             rename=rename)
    if profile.scope == "project" and profile.project_id == project_id and profile.person_id is None:
        prof = db.get(SpeakerProfile, profile.id)
        prof.person_id = person.id
        db.commit()
    return person, profile
