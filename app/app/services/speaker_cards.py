"""Карточка голоса на странице «Голоса» (/report-speaker-match): имя, компания, сторона и должность сразу.

Подтверждение голоса заводит или правит человека в реестре проекта («Люди и стороны»), чтобы менеджеру не приходилось
второй раз заполнять его на странице проекта. Компания и сторона обязательны; должность — по желанию. Если система уже
догадывается, кто это (реестр проекта, карточка сотрудника по голосу или по имени), поля заполняются, но голос принимается
только по кнопке человека — и узнанные автоматически тоже (``needs_human`` в match, см. ``speaker_auto``).

Человек без учётки получает отметку «согласие на голос получено» с основанием «договор» (решение 2026-10-15), кроме тех,
у кого согласие отзывали: отзыв — только консоль (Д4), повторно здесь не ставится.

Сведения о людях других проектов не используются (ПФ8): подсказка — только из реестра этого проекта и карточек
сотрудников исполнителя.
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
    cards: dict[str, Any] = {}
    for label, row in (payload.get("matches") or {}).items():
        if not isinstance(row, dict) or merge_map.get(label, label) != label:
            continue
        status = row.get("status") or "new"
        name = (row.get("profile_name") or "").strip() if status == "confirmed" else ""
        hint = suggest(db, project_id, name, profile_id=row.get("profile_id") if status == "confirmed" else None, staff=staff) if name else None
        cards[label] = {"status": status, "needs_human": bool(row.get("needs_human")), "suggestion": hint}
    return {
        "artifact_id": artifact.artifact_id,
        "people": [_person_row(p, sorted(set(keys.get(p.id, [])) | {people.alias_key(p.display_name)})) for p in persons],
        "staff": [{k: s[k] for k in ("name", "keys", "company", "side", "job_title")} for s in staff],
        "companies": sorted(companies.values(), key=str.casefold),
        "company_sides": sides,
        "cards": cards,
    }


def _consent_withdrawn(db: Session, person_id: int) -> bool:
    return db.scalar(select(PeopleLog.id).where(PeopleLog.person_id == person_id, PeopleLog.action == "voice_withdraw").limit(1)) is not None


def _upsert_person(db: Session, project_id: int, *, name: str, company: str, side: str, job_title: str | None,
                   by_user_id: int | None) -> Person:
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
        if people.alias_key(name) != people.alias_key(person.display_name):
            people.add_alias(db, project_id, person.id, name, source="voice", by_user_id=by_user_id)
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
                 job_title: str | None = None, by_user_id: int | None = None):
    """Подтвердить голос человеком: реестр проекта (человек, сторона компании, согласие) → запомнить голос под его именем."""
    from app.app.models.speaker_profile import SpeakerProfile
    from app.app.services.speaker_profiles import enroll_speaker

    name, company = " ".join((name or "").split()), " ".join((company or "").split())
    job_title = " ".join((job_title or "").split()) or None
    if not name:
        raise people.PeopleError("Введите имя спикера")
    if not company:
        raise people.PeopleError("Укажите компанию спикера")
    if side not in CARD_SIDES:
        raise people.PeopleError("Укажите сторону: исполнитель или заказчик")
    project_id = artifact.project_id
    try:
        person = _upsert_person(db, project_id, name=name, company=company, side=side, job_title=job_title, by_user_id=by_user_id)
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
                             display_name=person.display_name)
    if profile.scope == "project" and profile.project_id == project_id and profile.person_id is None:
        prof = db.get(SpeakerProfile, profile.id)
        prof.person_id = person.id
        db.commit()
    return person, profile
