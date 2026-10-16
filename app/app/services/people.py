"""Реестр людей проекта (docs/plans/PLATFORM-FOUNDATION-PLAN.md, ПФ2).

Извлечение и контракт называют человека строкой («Анна», «Ivan Sample»). Реестр превращает строку в человека:
``resolve`` ищет по алиасу, сторона (исполнитель / заказчик) берётся у человека, а не угадывается по роли.
Изменения — только через функции этого модуля: каждая пишет запись в ``people_log`` (Д6).
Словарь сторон контракта — ``customer`` / ``vendor`` (исполнитель); в БД — ``customer`` / ``executor``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.person import (
    ACTOR_KINDS,
    AUTHORITIES,
    SIDE_CUSTOMER,
    SIDE_EXECUTOR,
    SIDE_UNKNOWN,
    SIDES,
    PeopleLog,
    Person,
    PersonAlias,
    ProjectParty,
    UnknownActor,
)

# Сила источника стороны: человек главнее карточки, карточка и список проекта главнее догадки модели.
SIDE_RANK = {"model": 0, "project_party": 1, "card": 2, "human": 3}
SIDE_RU = {SIDE_EXECUTOR: "исполнитель", SIDE_CUSTOMER: "заказчик"}


class PeopleError(ValueError):
    """Ошибка правки реестра; текст можно показывать пользователю."""


def alias_key(text: str | None) -> str:
    """Ключ написания: без регистра, ё = е, знаки препинания и повторные пробелы убраны."""
    low = (text or "").casefold().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]+", " ", low).strip()


def side_to_contract(side: str | None) -> str | None:
    """Сторона в словаре контракта: customer / vendor; неизвестно — None."""
    return {SIDE_CUSTOMER: "customer", SIDE_EXECUTOR: "vendor"}.get(side or "")


def side_from_contract(side: str | None) -> str:
    return {"customer": SIDE_CUSTOMER, "vendor": SIDE_EXECUTOR}.get(side or "", SIDE_UNKNOWN)


@dataclass(frozen=True)
class Match:
    person: Person
    how: str  # exact | first_name


def _log(db: Session, project_id: int, person_id: int | None, user_id: int | None, action: str, before: Any, after: Any) -> None:
    db.add(PeopleLog(
        project_id=project_id, person_id=person_id, user_id=user_id, action=action,
        before_json=None if before is None else json.dumps(before, ensure_ascii=False, default=str),
        after_json=None if after is None else json.dumps(after, ensure_ascii=False, default=str),
    ))


def _snapshot(p: Person) -> dict[str, Any]:
    return {"display_name": p.display_name, "company_name": p.company_name, "side": p.side, "side_source": p.side_source,
            "job_title": p.job_title, "authority": p.authority, "user_id": p.user_id, "merged_into": p.merged_into,
            "is_active": p.is_active}


def canonical(db: Session, person: Person) -> Person:
    """Человек, в которого слит данный (по цепочке ``merged_into``); защита от циклов."""
    seen: set[int] = set()
    while person.merged_into and person.id not in seen:
        seen.add(person.id)
        nxt = db.get(Person, person.merged_into)
        if nxt is None:
            break
        person = nxt
    return person


def normalize_company(db: Session, company_name: str | None, company_id: int | None = None) -> tuple[int | None, str | None]:
    """Название компании человека к виду общего списка: «acme» и «ACME» — одна компания.
    Нет в списке — название остаётся как ввели (заказчик может быть только в этом проекте)."""
    from app.app.services.companies import canonical_company

    name = " ".join((company_name or "").split()) or None
    row = canonical_company(db, name)
    if row is not None:
        return row.id, row.name
    return company_id, name


def normalize_project_companies(db: Session, project_id: int) -> int:
    """Привести компании всех людей проекта к общему списку; возвращает, у скольких людей что-то поменялось."""
    changed = 0
    for person in list_people(db, project_id, include_merged=True):
        cid, cname = normalize_company(db, person.company_name, person.company_id)
        if (cid, cname) != (person.company_id, person.company_name):
            person.company_id, person.company_name = cid, cname
            changed += 1
    db.flush()
    return changed


def user_taken(db: Session, project_id: int, user_id: int) -> bool:
    """Учётка уже привязана к человеку проекта."""
    return db.scalar(select(Person.id).where(Person.project_id == project_id, Person.user_id == user_id, Person.merged_into.is_(None))) is not None


def get(db: Session, project_id: int, person_id: int) -> Person:
    person = db.get(Person, person_id)
    if person is None or person.project_id != project_id:
        raise PeopleError("Человек не найден")
    return person


def list_people(db: Session, project_id: int, *, include_merged: bool = False) -> list[Person]:
    q = select(Person).where(Person.project_id == project_id)
    if not include_merged:
        q = q.where(Person.merged_into.is_(None), Person.is_active.is_(True))
    return list(db.scalars(q.order_by(Person.display_name)).all())


def aliases_of(db: Session, person_id: int) -> list[PersonAlias]:
    return list(db.scalars(select(PersonAlias).where(PersonAlias.person_id == person_id).order_by(PersonAlias.id)).all())


def create_person(
    db: Session, project_id: int, display_name: str, *, user_id: int | None = None, company_id: int | None = None,
    company_name: str | None = None, side: str = SIDE_UNKNOWN, side_source: str = "model", job_title: str | None = None,
    authority: str | None = None, by_user_id: int | None = None, alias_source: str = "human",
) -> Person:
    name = " ".join((display_name or "").split())
    if not name or not alias_key(name):
        raise PeopleError("Имя не может быть пустым")
    if side not in SIDES:
        raise PeopleError("Неизвестная сторона")
    if authority is not None and authority not in AUTHORITIES:
        raise PeopleError("Неизвестное право утверждения")
    company_id, company_name = normalize_company(db, company_name, company_id)
    taken = db.scalar(select(PersonAlias).where(PersonAlias.project_id == project_id, PersonAlias.alias_key == alias_key(name)))
    if taken is not None:
        raise PeopleError("Такое имя уже есть в реестре проекта")
    if user_id is not None:
        exists = db.scalar(select(Person.id).where(Person.project_id == project_id, Person.user_id == user_id, Person.merged_into.is_(None)))
        if exists is not None:
            raise PeopleError("Эта учётка уже привязана к человеку проекта")
    person = Person(project_id=project_id, user_id=user_id, display_name=name, company_id=company_id, company_name=company_name,
                    side=side, side_source=side_source, job_title=job_title, authority=authority)
    db.add(person)
    db.flush()
    db.add(PersonAlias(project_id=project_id, person_id=person.id, alias=name, alias_key=alias_key(name), source=alias_source))
    _log(db, project_id, person.id, by_user_id, "create", None, _snapshot(person))
    db.flush()
    align_to_company(db, project_id, person, by_user_id=by_user_id)
    attach_words(db, project_id, person)
    return person


def add_alias(db: Session, project_id: int, person_id: int, alias: str, *, source: str = "human", by_user_id: int | None = None) -> PersonAlias:
    person = get(db, project_id, person_id)
    key = alias_key(alias)
    if not key:
        raise PeopleError("Пустое написание")
    row = db.scalar(select(PersonAlias).where(PersonAlias.project_id == project_id, PersonAlias.alias_key == key))
    if row is not None:
        if row.person_id == person.id:
            return row
        raise PeopleError("Это написание уже принадлежит другому человеку; их можно объединить")
    row = PersonAlias(project_id=project_id, person_id=person.id, alias=" ".join(alias.split()), alias_key=key, source=source)
    db.add(row)
    _log(db, project_id, person.id, by_user_id, "add_alias", None, {"alias": row.alias, "source": source})
    db.flush()
    attach_words(db, project_id, person)
    return row

def attach_words(db: Session, project_id: int, person: Person) -> int:
    """Слова человека — к его карточке: уже разобранные реплики (``speech_acts``) без человека, автор которых — одно из
    написаний человека, привязываются к нему (карточку или написание завели позже разбора материала). Сторона реплики,
    если неизвестна, — сторона человека."""
    from app.app.models.speech_act import SpeechAct

    keys = {a.alias_key for a in aliases_of(db, person.id)}
    side = person.side if person.side in (SIDE_EXECUTOR, SIDE_CUSTOMER) else None
    n = 0
    for act in db.scalars(select(SpeechAct).where(SpeechAct.project_id == project_id, SpeechAct.person_id.is_(None),
                                                  SpeechAct.author.isnot(None))).all():
        if alias_key(act.author) in keys:
            act.person_id = person.id
            if side and act.side in (None, "", SIDE_UNKNOWN):
                act.side = side
            n += 1
    if n:
        db.flush()
        sync_decision_memory(db, project_id, person_ids={person.id})
    return n

def sync_decision_memory(db: Session, project_id: int, *, person_ids: set[int] | None = None,
                         subject_ids: set[int] | None = None) -> dict[str, int]:
    """Правки людей — в память решений: событие журнала, выведенное из реплики, получает человека и сторону этой реплики
    (реплику привязали к карточке позже, карточки объединили, сторону исправили по компании); затронутые предметы
    пересчитываются свёрткой журнала (``decision_log.refresh_subject``, модель не вызывается) — кроме предметов, итог которых
    поставил судья или человек: их не трогаем, у них меняются только события (судья перепроверит). ``person_ids`` — только
    события реплик этих людей; None — весь проект."""
    from app.app.models.decision_memory import DecisionEvent, Subject
    from app.app.models.speech_act import SpeechAct
    from app.app.services import decision_log

    touched = set(subject_ids or ())
    q = (select(DecisionEvent, SpeechAct).join(SpeechAct, SpeechAct.id == DecisionEvent.speech_act_id)
         .where(DecisionEvent.project_id == project_id, SpeechAct.person_id.isnot(None)))
    if person_ids is not None:
        q = q.where(SpeechAct.person_id.in_(person_ids or {-1}))
    events = 0
    for event, act in (db.execute(q).all() if person_ids is None or person_ids else []):
        side = act.side if act.side in (SIDE_EXECUTOR, SIDE_CUSTOMER) else event.actor_side
        if (event.actor_person_id, event.actor_side) != (act.person_id, side):
            event.actor_person_id, event.actor_side = act.person_id, side
            touched.add(event.subject_id)
            events += 1
    db.flush()
    changed = 0
    for sid in sorted(touched):
        subject = db.get(Subject, sid)
        if subject is None or subject.state_source in ("judge", "human"):
            continue  # итог поставил судья (модель) или человек — свёртка его не перетирает; судья перепроверит сам
        before = subject.state
        if decision_log.refresh_subject(db, subject).state != before:
            changed += 1
    db.flush()
    return {"events": events, "subjects": len(touched), "changed": changed}


def resolve(db: Session, project_id: int, label: str | None, *, allow_first_name: bool = True) -> Match | None:
    """Человек по написанию. Точный алиас — надёжно; «Анна» вместо «Анна Пример» — только если такое имя одно."""
    key = alias_key(label)
    if not key:
        return None
    row = db.scalar(select(PersonAlias).where(PersonAlias.project_id == project_id, PersonAlias.alias_key == key))
    if row is not None:
        person = db.get(Person, row.person_id)
        if person is not None:
            return Match(canonical(db, person), "exact")
    if allow_first_name and " " not in key:
        owners: dict[int, Person] = {}
        for alias_row in db.scalars(select(PersonAlias).where(PersonAlias.project_id == project_id, PersonAlias.alias_key.like(f"{key} %"))).all():
            person = db.get(Person, alias_row.person_id)
            if person is not None:
                owner = canonical(db, person)
                owners[owner.id] = owner
        if len(owners) == 1:
            return Match(next(iter(owners.values())), "first_name")
    return None


def set_side(db: Session, project_id: int, person_id: int, side: str, *, source: str, by_user_id: int | None = None, force: bool = False) -> Person:
    """Сторона человека. Источник слабее прежнего не перебивает (модель не переписывает решение человека) — если не ``force``."""
    if side not in SIDES:
        raise PeopleError("Неизвестная сторона")
    person = get(db, project_id, person_id)
    # Р1: у человека с компанией сторону задаёт компания в проекте
    party = find_party(db, project_id, person.company_name) if (person.company_name or "").strip() else None
    if party is not None:
        if side != party.side and source == "human":
            raise PeopleError(f"«{party.company_name}» в этом проекте — {SIDE_RU.get(party.side, party.side)}. "
                              "Сторону меняют у компании в блоке «Стороны компаний».")
        return align_to_company(db, project_id, person, by_user_id=by_user_id)
    if (person.company_name or "").strip() and side in (SIDE_EXECUTOR, SIDE_CUSTOMER) and source == "human":
        set_party(db, project_id, person.company_name, side, by_user_id=by_user_id)  # компания входит в проект с этой стороной
        return align_to_company(db, project_id, person, by_user_id=by_user_id)
    if not force and SIDE_RANK.get(source, 0) < SIDE_RANK.get(person.side_source, 0) and person.side != SIDE_UNKNOWN:
        return person
    before = _snapshot(person)
    person.side, person.side_source = side, source
    _log(db, project_id, person.id, by_user_id, "set_side", before, _snapshot(person))
    db.flush()
    return person


def update_person(db: Session, project_id: int, person_id: int, *, by_user_id: int | None = None, **fields: Any) -> Person:
    """Правка карточки человека проекта: company_name, company_id, job_title, authority, display_name, user_id."""
    person = get(db, project_id, person_id)
    allowed = {"company_name", "company_id", "job_title", "authority", "display_name", "user_id", "company_unconfirmed",
               "needs_review"}
    bad = set(fields) - allowed
    if bad:
        raise PeopleError(f"Нельзя менять: {', '.join(sorted(bad))}")
    if fields.get("authority") is not None and fields["authority"] not in AUTHORITIES:
        raise PeopleError("Неизвестное право утверждения")
    if "user_id" in fields and fields["user_id"] is not None:
        other = db.scalar(select(Person.id).where(Person.project_id == project_id, Person.user_id == fields["user_id"],
                                                  Person.id != person.id, Person.merged_into.is_(None)))
        if other is not None:
            raise PeopleError("Эта учётка уже привязана к другому человеку проекта")
    if "company_name" in fields:
        fields["company_id"], fields["company_name"] = normalize_company(db, fields["company_name"], fields.get("company_id"))
    before = _snapshot(person)
    for key, value in fields.items():
        setattr(person, key, value)
    if "display_name" in fields and fields["display_name"]:
        add_alias(db, project_id, person.id, fields["display_name"], source="human", by_user_id=by_user_id)
    _log(db, project_id, person.id, by_user_id, "update", before, _snapshot(person))
    db.flush()
    if "company_name" in fields:
        align_to_company(db, project_id, person, by_user_id=by_user_id)
    return person


def merge(db: Session, project_id: int, keep_id: int, drop_id: int, *, by_user_id: int | None = None) -> Person:
    """«Это один человек»: алиасы, голоса, участия, вопросы «Кто это?», уже разобранные реплики и события памяти решений
    переходят к ``keep``; ``drop`` остаётся с ``merged_into``. Ник из переписки и имя из созвона после этого — один человек:
    реплики под любым из его написаний (и прежде не опознанные) — его, со стороной оставшейся карточки."""
    if keep_id == drop_id:
        raise PeopleError("Нельзя объединить человека с самим собой")
    keep, drop = get(db, project_id, keep_id), get(db, project_id, drop_id)
    if drop.merged_into or keep.merged_into:
        raise PeopleError("Один из людей уже объединён")
    if keep.user_id and drop.user_id and keep.user_id != drop.user_id:
        raise PeopleError("У людей разные учётки: объединять нельзя")
    before = {"keep": _snapshot(keep), "drop": _snapshot(drop)}
    for row in aliases_of(db, drop.id):
        row.person_id = keep.id
    if drop.user_id and not keep.user_id:
        keep.user_id, drop.user_id = drop.user_id, None
    if keep.side == SIDE_UNKNOWN or SIDE_RANK.get(drop.side_source, 0) > SIDE_RANK.get(keep.side_source, 0) and drop.side != SIDE_UNKNOWN:
        keep.side, keep.side_source = drop.side, drop.side_source
    keep.company_name = keep.company_name or drop.company_name
    keep.company_id = keep.company_id or drop.company_id
    keep.job_title = keep.job_title or drop.job_title
    keep.authority = keep.authority or drop.authority
    drop.merged_into, drop.is_active = keep.id, False
    from app.app.models.meeting import MeetingParticipant
    from app.app.models.speaker_profile import SpeakerProfile

    for model in (SpeakerProfile, MeetingParticipant):
        for row in db.scalars(select(model).where(model.person_id == drop.id)).all():
            row.person_id = keep.id
    for row in db.scalars(select(UnknownActor).where(UnknownActor.person_id == drop.id)).all():
        row.person_id = keep.id
    _log(db, project_id, keep.id, by_user_id, "merge", before, {"keep": _snapshot(keep), "drop_id": drop.id})
    db.flush()
    keep = align_to_company(db, project_id, keep, by_user_id=by_user_id)
    keep.needs_review = keep.needs_review and drop.needs_review  # объединили с известным — карточка разобрана
    _rebind_material(db, project_id, keep, drop.id)
    return keep


def _rebind_material(db: Session, project_id: int, keep: Person, drop_id: int) -> None:
    """Реплики (``speech_acts``) и события памяти решений (``decision_events``) человека ``drop_id`` — к ``keep``;
    не опознанные раньше реплики под написанием ``keep`` — тоже к нему. Неизвестная сторона реплики — сторона ``keep``."""
    from app.app.models.decision_memory import DecisionEvent
    from app.app.models.speech_act import SpeechAct

    keys = {a.alias_key for a in aliases_of(db, keep.id)}
    side = keep.side if keep.side in (SIDE_EXECUTOR, SIDE_CUSTOMER) else None
    acts = db.scalars(select(SpeechAct).where(SpeechAct.project_id == project_id,
                                              (SpeechAct.person_id == drop_id) | SpeechAct.person_id.is_(None))).all()
    for act in acts:
        if act.person_id is None and alias_key(act.author) not in keys:
            continue
        act.person_id = keep.id
        if side and act.side in (None, "", SIDE_UNKNOWN):
            act.side = side
    subjects = set()
    for event in db.scalars(select(DecisionEvent).where(DecisionEvent.project_id == project_id,
                                                        DecisionEvent.actor_person_id == drop_id)).all():
        event.actor_person_id = keep.id
        if side and event.actor_side in (None, "", SIDE_UNKNOWN):
            event.actor_side = side
        subjects.add(event.subject_id)
    db.flush()
    sync_decision_memory(db, project_id, person_ids={keep.id}, subject_ids=subjects)


# ---------------------------------------------------------------- стороны проекта

def party_side(db: Session, project_id: int, company_name: str | None) -> str | None:
    """Сторона компании в этом проекте (Д1): ``executor`` / ``customer`` или None, если компания не задана."""
    row = find_party(db, project_id, company_name)
    return row.side if row is not None else None


def find_party(db: Session, project_id: int, company_name: str | None) -> ProjectParty | None:
    """Сторона проекта по названию компании: то же написание, иначе та же компания списка («acme» и «ООО ACME»)."""
    from app.app.services.companies import canonical_company, loose_key

    key = alias_key(company_name)
    if not key:
        return None
    rows = db.scalars(select(ProjectParty).where(ProjectParty.project_id == project_id)).all()
    hit = next((r for r in rows if r.company_key == key), None)
    if hit is not None:
        return hit
    company = canonical_company(db, company_name)
    loose = loose_key(company_name)
    return next((r for r in rows if (company is not None and r.company_id == company.id) or (loose and loose_key(r.company_name) == loose)), None)


def set_party(db: Session, project_id: int, company_name: str, side: str, *, company_id: int | None = None, by_user_id: int | None = None) -> ProjectParty:
    """Сторона компании в проекте (Д1). Компания всегда из общего списка: новая попадает туда со стороной по роли (Р2),
    сторона проекта хранит её ``company_id`` и написание из списка. ``company_id`` вызывающего больше не нужен."""
    from app.app.services.companies import CompanyError, ensure_company

    if side not in (SIDE_EXECUTOR, SIDE_CUSTOMER):
        raise PeopleError("Сторона компании: исполнитель или заказчик")
    if not alias_key(company_name):
        raise PeopleError("Пустое название компании")
    try:
        company = ensure_company(db, company_name, side)
    except CompanyError as exc:
        raise PeopleError(str(exc)) from exc
    row = find_party(db, project_id, company.name) or find_party(db, project_id, company_name)
    before = None if row is None else {"company": row.company_name, "side": row.side}
    if row is None:
        row = ProjectParty(project_id=project_id, company_id=company.id, company_name=company.name,
                           company_key=alias_key(company.name), side=side)
        db.add(row)
    else:
        row.side, row.company_id, row.company_name, row.company_key = side, company.id, company.name, alias_key(company.name)
    _log(db, project_id, None, by_user_id, "set_party", before, {"company": row.company_name, "side": side})
    db.flush()
    for person in list_people(db, project_id):  # Р1: люди компании получают её сторону
        if (person.company_name or "").strip() and find_party(db, project_id, person.company_name) is row:
            _take_party(db, project_id, person, row, by_user_id)
    db.flush()
    return row


def _take_party(db: Session, project_id: int, person: Person, party: ProjectParty, by_user_id: int | None) -> Person:
    """Сторона и написание компании человека — как у стороны проекта; запись в журнал, если что-то изменилось."""
    if (person.side, person.side_source, person.company_name, person.company_id) == (party.side, "project_party", party.company_name, party.company_id):
        return person
    before = _snapshot(person)
    side_changed = person.side != party.side
    person.side, person.side_source = party.side, "project_party"
    person.company_name, person.company_id = party.company_name, party.company_id
    _log(db, project_id, person.id, by_user_id, "side_from_party", before, _snapshot(person))
    db.flush()
    if side_changed:
        _resync_person_side(db, project_id, person)
    return person


def _resync_person_side(db: Session, project_id: int, person: Person) -> None:
    """Сторона человека исправлена (Р1: от компании) — его реплики и события журнала получают её, предметы пересчитываются."""
    from app.app.models.speech_act import SpeechAct

    if person.side not in (SIDE_EXECUTOR, SIDE_CUSTOMER):
        return
    for act in db.scalars(select(SpeechAct).where(SpeechAct.project_id == project_id, SpeechAct.person_id == person.id)).all():
        act.side = person.side
    db.flush()
    sync_decision_memory(db, project_id, person_ids={person.id})


def align_to_company(db: Session, project_id: int, person: Person, *, by_user_id: int | None = None) -> Person:
    """Р1 (PEOPLE-FLOW-PLAN, ЛГ2): сторона человека равна стороне его компании в проекте. Компании ещё нет в сторонах
    проекта, а сторона человека известна — компания входит в проект с этой стороной; сторона неизвестна — ждём человека."""
    if not (person.company_name or "").strip():
        return person
    party = find_party(db, project_id, person.company_name)
    if party is None:
        if person.side not in (SIDE_EXECUTOR, SIDE_CUSTOMER):
            return person
        set_party(db, project_id, person.company_name, person.side, by_user_id=by_user_id)  # сама выровняет людей компании
        return person
    return _take_party(db, project_id, person, party, by_user_id)


# ---------------------------------------------------------------- вопросы «Кто это?»

def note_unknown(db: Session, project_id: int, kind: str, label: str, *, artifact_id: int | None = None, example: str | None = None,
                 suggestions: list[dict[str, Any]] | None = None) -> UnknownActor:
    """Занести неопознанное написание в очередь «Кто это?» (повтор дополняет примеры, а не создаёт вторую строку)."""
    if kind not in ACTOR_KINDS:
        raise PeopleError("Неизвестный вид")
    key = alias_key(label)
    if not key:
        raise PeopleError("Пустая метка")
    row = db.scalar(select(UnknownActor).where(UnknownActor.project_id == project_id, UnknownActor.kind == kind,
                                               UnknownActor.label_key == key, UnknownActor.artifact_id == artifact_id))
    if row is None:
        row = UnknownActor(project_id=project_id, artifact_id=artifact_id, kind=kind, label=" ".join(label.split()), label_key=key)
        db.add(row)
    examples = json.loads(row.examples_json or "[]")
    if example and example not in examples and len(examples) < 5:
        examples.append(example[:300])
    row.examples_json = json.dumps(examples, ensure_ascii=False)
    if suggestions is not None:
        row.suggestions_json = json.dumps(suggestions, ensure_ascii=False)
    db.flush()
    return row


def resolve_unknown(db: Session, project_id: int, actor_id: int, *, person_id: int | None, ignore: bool = False, by_user_id: int | None = None) -> UnknownActor:
    """«Это X» (person_id) или «Не участник» (ignore). Написание становится алиасом человека, чтобы больше не спрашивать."""
    from datetime import datetime, timezone

    row = db.get(UnknownActor, actor_id)
    if row is None or row.project_id != project_id:
        raise PeopleError("Запись не найдена")
    if ignore:
        row.status = "ignored"
    else:
        if person_id is None:
            raise PeopleError("Не выбран человек")
        person = get(db, project_id, person_id)
        if row.kind in ("chat_author", "document_author", "guest"):  # метка speaker_N живёт в одном материале — алиасом не делаем
            add_alias(db, project_id, person.id, row.label, source="chat" if row.kind == "chat_author" else "room" if row.kind == "guest" else "human",
                      by_user_id=by_user_id)
        row.person_id, row.status = person.id, "resolved"
        if row.kind == "guest":
            attach_guest_rows(db, project_id, row.label_key, person.id)
    row.resolved_by, row.resolved_at = by_user_id, datetime.now(timezone.utc)
    _log(db, project_id, row.person_id, by_user_id, "resolve_unknown", {"label": row.label, "kind": row.kind}, {"status": row.status, "person_id": row.person_id})
    db.flush()
    return row


def open_unknown(db: Session, project_id: int) -> list[UnknownActor]:
    return list(db.scalars(select(UnknownActor).where(UnknownActor.project_id == project_id, UnknownActor.status == "open")
                           .order_by(UnknownActor.id)).all())


# ---------------------------------------------------------------- для контракта

def sides_map(db: Session, project_id: int) -> dict[str, str]:
    """Словарь «написание -> customer / vendor» для ``contract._resolve_side``: все алиасы людей с известной стороной."""
    out: dict[str, str] = {}
    for person, alias in db.execute(
        select(Person, PersonAlias).join(PersonAlias, PersonAlias.person_id == Person.id)
        .where(Person.project_id == project_id, Person.merged_into.is_(None), Person.side != SIDE_UNKNOWN)
    ).all():
        contract_side = side_to_contract(person.side)
        if contract_side:
            out[alias.alias.casefold()] = contract_side
    # «Анна» вместо «Анна Пример»: первое слово годится, только если у всех людей с таким первым словом одна сторона
    firsts: dict[str, set[str]] = {}
    for name, side in out.items():
        if " " in name:
            firsts.setdefault(name.split()[0], set()).add(side)
    for word, sides in firsts.items():
        if len(sides) == 1 and word not in out:
            out[word] = next(iter(sides))
    return out


def member_person(db: Session, project_id: int, user_id: int, *, by_user_id: int | None = None) -> Person | None:
    """ЛГ3: участник проекта — человек проекта. Уже есть человек с этой учёткой — он; есть человек без учётки с тем же
    именем, что в карточке, — ему привязывается учётка; иначе новый по карточке. Без имени в карточке — None."""
    from app.app.services import user_profile as profiles

    own = db.scalar(select(Person).where(Person.project_id == project_id, Person.user_id == user_id, Person.merged_into.is_(None)))
    if own is not None:
        return own
    card = profiles.get_profile(db, user_id)
    if card is None or not (card.first_name and card.last_name):
        return None
    for name in (f"{card.first_name} {card.last_name}", f"{card.last_name} {card.first_name}"):
        hit = resolve(db, project_id, name, allow_first_name=False)
        if hit is not None and hit.person.user_id is None:
            return update_person(db, project_id, hit.person.id, by_user_id=by_user_id, user_id=user_id)
    return person_from_user(db, project_id, user_id, by_user_id=by_user_id)


def sync_user_company(db: Session, user_id: int, old_company_id: int | None, *, by_user_id: int | None = None) -> int:
    """Компания карточки сотрудника → его люди во всех проектах, у кого компания пустая или прежняя карточки."""
    from app.app.models.company import Company
    from app.app.services import user_profile as profiles

    card = profiles.get_profile(db, user_id)
    company = db.get(Company, card.company_id) if card is not None and card.company_id else None
    if company is None:
        return 0
    changed = 0
    for person in db.scalars(select(Person).where(Person.user_id == user_id, Person.merged_into.is_(None))).all():
        if (person.company_name or "").strip() and not (old_company_id and person.company_id == old_company_id):
            continue
        if person.company_id == company.id:
            continue
        if find_party(db, person.project_id, company.name) is None and company.side in (SIDE_EXECUTOR, SIDE_CUSTOMER):
            set_party(db, person.project_id, company.name, company.side, by_user_id=by_user_id)  # сторона компании по умолчанию
        update_person(db, person.project_id, person.id, by_user_id=by_user_id, company_name=company.name)
        changed += 1
    db.flush()
    return changed


def person_from_user(db: Session, project_id: int, user_id: int, *, by_user_id: int | None = None) -> Person:
    """Сотрудник портала -> человек проекта: имя, компания и должность из карточки, сторона от компании проекта или списка."""
    from app.app.models.company import Company
    from app.app.models.user import User
    from app.app.services import user_profile as profiles

    card = profiles.get_profile(db, user_id)
    account = db.get(User, user_id)
    if card is not None and (card.first_name or card.last_name):
        name = f"{card.first_name} {card.last_name}".strip()
        company = db.get(Company, card.company_id) if card.company_id else None
    else:
        name, company = (account.name if account else ""), None
    side = party_side(db, project_id, company.name if company else None) or (company.side if company and company.side else SIDE_UNKNOWN)
    person = create_person(db, project_id, name, user_id=user_id, company_name=company.name if company else None,
                           side=side, side_source="card" if card is not None else "human", job_title=card.job_title if card else None,
                           by_user_id=by_user_id, alias_source="card")
    if card is not None and card.last_name and card.first_name:
        try:
            add_alias(db, project_id, person.id, f"{card.last_name} {card.first_name}", source="card", by_user_id=by_user_id)
        except PeopleError:
            pass
    return person


def move_user_persons(db: Session, *, winner_id: int, loser_id: int) -> int:
    """Объединение учёток: люди проектов с учёткой ``loser`` переходят к ``winner``; если у ``winner`` в проекте уже есть
    человек, два человека объединяются (учётка одна на человека проекта)."""
    moved = 0
    for person in list(db.scalars(select(Person).where(Person.user_id == loser_id)).all()):
        other = db.scalar(select(Person).where(Person.project_id == person.project_id, Person.user_id == winner_id, Person.merged_into.is_(None)))
        if other is not None and other.id != person.id:
            person.user_id = None
            merge(db, person.project_id, other.id, person.id, by_user_id=winner_id)
        else:
            person.user_id = winner_id
        moved += 1
    db.flush()
    return moved


# ---------------------------------------------------------------- согласие на голос внешнего человека (Д5)

def give_voice_consent(db: Session, project_id: int, person_id: int, *, by_user_id: int | None = None, basis: str | None = None) -> Person:
    """Менеджер отмечает, что внешний человек (без учётки) согласен на запись голоса. У сотрудника согласие — в его карточке."""
    from datetime import datetime, timezone

    person = get(db, project_id, person_id)
    if person.user_id is not None:
        raise PeopleError("У сотрудника согласие на голос даётся в его карточке")
    before = _snapshot(person)
    person.voice_consent_at, person.voice_consent_by = datetime.now(timezone.utc), by_user_id
    _log(db, project_id, person.id, by_user_id, "voice_consent", before, {"voice_consent_at": person.voice_consent_at.isoformat(), "basis": basis})
    db.flush()
    return person


def withdraw_voice_consent(db: Session, project_id: int, person_id: int, *, by_user_id: int | None = None) -> int:
    """Отзыв согласия (только консоль, Д4): слепки этого человека в проекте удаляются, отметка снимается. Возвращает число слепков."""
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile

    person = get(db, project_id, person_id)
    if person.user_id is not None:
        raise PeopleError("У сотрудника согласие отзывается в его карточке")
    profiles = list(db.scalars(select(SpeakerProfile).where(SpeakerProfile.person_id == person.id)).all())
    ids = [p.id for p in profiles]
    if ids:
        for link in db.scalars(select(ArtifactSpeakerLink).where(ArtifactSpeakerLink.profile_id.in_(ids))).all():
            link.profile_id, link.status = None, "pending"
        for link in db.scalars(select(ArtifactSpeakerLink).where(ArtifactSpeakerLink.suggested_profile_id.in_(ids))).all():
            link.suggested_profile_id = None
    for p in profiles:
        db.delete(p)
    before = _snapshot(person)
    person.voice_consent_at = person.voice_consent_by = None
    _log(db, project_id, person.id, by_user_id, "voice_withdraw", before, {"profiles_deleted": len(ids)})
    db.flush()
    return len(ids)


# ---------------------------------------------------------------- участники комнаты (ПФ7)

def attach_guest_rows(db: Session, project_id: int, label_key: str, person_id: int) -> int:
    """Все записи гостя с этим написанием во встречах проекта получают человека (повторные встречи — без вопросов)."""
    from app.app.models.meeting import Meeting, MeetingParticipant

    rows = db.execute(
        select(MeetingParticipant).join(Meeting, Meeting.id == MeetingParticipant.meeting_id)
        .where(Meeting.project_id == project_id, MeetingParticipant.user_id.is_(None), MeetingParticipant.person_id.is_(None))
    ).scalars().all()
    n = 0
    for row in rows:
        if alias_key(row.display_name) == label_key:
            row.person_id = person_id
            n += 1
    db.flush()
    return n


def link_meeting_participants(db: Session, meeting: Any, *, by_user_id: int | None = None) -> dict[str, int]:
    """После встречи: каждый участник комнаты становится человеком проекта или вопросом «Кто это?».
    Сотрудник (учётка) — человек по карточке (создаётся, если его ещё нет); гость — по написанию имени в реестре, иначе
    новый человек проекта с согласием на голос (Р7) и компанией, указанной при входе; вопрос — только если одиночное имя
    подходит нескольким людям.
    Повтор безопасен: уже связанные участники пропускаются, вопросы не дублируются."""
    from app.app.models.meeting import MeetingParticipant

    out = {"linked": 0, "created": 0, "unknown": 0}
    rows = db.scalars(select(MeetingParticipant).where(MeetingParticipant.meeting_id == meeting.id).order_by(MeetingParticipant.id)).all()
    for row in rows:
        if row.person_id is not None:
            continue
        person = None
        if row.user_id is not None:
            person = db.scalar(select(Person).where(Person.project_id == meeting.project_id, Person.user_id == row.user_id, Person.merged_into.is_(None)))
            if person is None:
                try:
                    with db.begin_nested():
                        person = person_from_user(db, meeting.project_id, row.user_id, by_user_id=by_user_id)
                    out["created"] += 1
                except PeopleError:  # имя уже занято другим человеком: ищем по написанию
                    person = None
        if person is None:
            hit = resolve(db, meeting.project_id, row.display_name)
            person = hit.person if hit is not None else None
        if person is None and row.user_id is None:
            staff_id = _staff_by_name(db, row.display_name)  # сотрудник зашёл гостем («Фамилия Имя Отчество» тоже)
            if staff_id is not None:
                try:
                    with db.begin_nested():
                        person = member_person(db, meeting.project_id, staff_id, by_user_id=by_user_id)
                except PeopleError:
                    person = None
        if person is None and row.user_id is None and not _ambiguous_first_name(db, meeting.project_id, row.display_name)                 and not _placeholder_guest(row.display_name):
            person = _guest_person(db, meeting, row, by_user_id)  # Р7: гость встречи — человек проекта
            if person is not None:
                out["created"] += 1
        if person is not None:
            _guest_company(db, meeting.project_id, person, row, by_user_id)
            row.person_id = person.id
            out["linked"] += 1
        else:
            note_unknown(db, meeting.project_id, "guest", row.display_name, example=f"Участник встречи №{meeting.id}")
            out["unknown"] += 1
    db.flush()
    return out


GUEST_CONSENT_BASIS = "согласие при входе во встречу"


def _placeholder_guest(label: str | None) -> bool:
    """«Гость», «Гость проверки …» — не имя человека: такой участник идёт в «Кто это?», человеком не заводится."""
    return alias_key(label).startswith("гость")


def _staff_by_name(db: Session, label: str | None) -> int | None:
    """Учётка сотрудника по имени гостя: «Имя Фамилия» или «Фамилия Имя», отчество отбрасывается; тёзки — не угадываем."""
    from app.app.models.user_profile import UserProfile

    tokens = alias_key(label).split()
    if len(tokens) < 2:
        return None
    wanted = {" ".join(tokens[:2]), " ".join(reversed(tokens[:2]))}
    hits = {c.user_id for c in db.scalars(select(UserProfile)).all()
            if c.first_name and c.last_name and alias_key(f"{c.first_name} {c.last_name}") in wanted}
    return next(iter(hits)) if len(hits) == 1 else None


def _ambiguous_first_name(db: Session, project_id: int, label: str | None) -> bool:
    """Одиночное имя («Анна»), под которое подходят несколько людей проекта, — вопрос «Кто это?», а не новый человек."""
    key = alias_key(label)
    if not key or " " in key:
        return False
    owners = {a.person_id for a in db.scalars(select(PersonAlias).where(PersonAlias.project_id == project_id,
                                                                      PersonAlias.alias_key.like(f"{key} %"))).all()}
    return len(owners) > 1


def _guest_person(db: Session, meeting: Any, row: Any, by_user_id: int | None) -> Person | None:
    """Р7 (PEOPLE-FLOW-PLAN, ЛГ4): гость вошёл с согласием на запись и назвался — он человек проекта. Компания — если
    указал её при входе и она есть в сторонах проекта (сторона — от неё, Р1); иначе без компании, сторону укажет менеджер."""
    party = find_party(db, meeting.project_id, row.company_name) if (row.company_name or "").strip() else None
    guessed = False
    if party is None and not (row.company_name or "").strip():
        party = default_party(db, meeting.project_id)  # правило владельца: не сотрудник — сотрудник заказчика проекта
        guessed = party is not None  # гость может быть и из другой компании — менеджер подтвердит или выберет другую
    try:
        with db.begin_nested():
            person = create_person(db, meeting.project_id, row.display_name, company_name=party.company_name if party else None,
                                   side=party.side if party else SIDE_UNKNOWN, side_source="project_party" if party else "model",
                                   by_user_id=by_user_id, alias_source="guest")
            give_voice_consent(db, meeting.project_id, person.id, by_user_id=by_user_id, basis=GUEST_CONSENT_BASIS)
            person.company_unconfirmed = guessed
            person.needs_review = True  # новая карточка от системы — подсвечена до подтверждения или объединения
        return person
    except PeopleError:
        return None


def default_party(db: Session, project_id: int) -> ProjectParty | None:
    """Компания по умолчанию для незнакомого человека проекта (правило владельца: не сотрудник — сотрудник заказчика):
    единственный заказчик проекта; у внутреннего проекта — единственный исполнитель. Ставится с пометкой «компания
    определена автоматически» — менеджер подтверждает или выбирает другую."""
    from app.app.models.project import Project

    project = db.get(Project, project_id)
    if project is None:
        return None
    side = SIDE_EXECUTOR if project.settings().get("internal_project") else SIDE_CUSTOMER
    rows = db.scalars(select(ProjectParty).where(ProjectParty.project_id == project_id, ProjectParty.side == side)).all()
    return rows[0] if len(rows) == 1 else None


def chat_author_people(db: Session, project_id: int, authors: list[str], *, by_user_id: int | None = None) -> dict[str, int]:
    """Авторы переписки — люди проекта (у каждой реплики должна быть карточка): известный по написанию — он; сотрудник по
    имени в карточке — его человек; иначе новая карточка с пометкой «Новая карточка» и компанией по умолчанию — менеджер
    подтверждает её или объединяет с уже известным человеком («Это один человек»). Ник остаётся написанием карточки."""
    from app.app.services.people_seed import clean_name

    out = {"known": 0, "staff": 0, "created": 0, "skipped": 0}
    for raw in dict.fromkeys(a for a in authors if (a or "").strip()):
        name = clean_name(raw)
        if not name:
            out["skipped"] += 1
            continue
        hit = resolve(db, project_id, raw) or resolve(db, project_id, name)
        if hit is not None:
            if alias_key(raw) != alias_key(hit.person.display_name):
                try:
                    add_alias(db, project_id, hit.person.id, raw, source="chat", by_user_id=by_user_id)
                except PeopleError:
                    pass
            attach_words(db, project_id, hit.person)
            out["known"] += 1
            continue
        staff_id = _staff_by_name(db, name)
        if staff_id is not None:
            try:
                with db.begin_nested():
                    person = member_person(db, project_id, staff_id, by_user_id=by_user_id)
                    if person is not None and alias_key(raw) != alias_key(person.display_name):
                        add_alias(db, project_id, person.id, raw, source="chat", by_user_id=by_user_id)
                if person is not None:
                    out["staff"] += 1
                    continue
            except PeopleError:
                pass
        party = default_party(db, project_id)
        try:
            with db.begin_nested():
                person = create_person(db, project_id, name, company_name=party.company_name if party else None,
                                       side=party.side if party else SIDE_UNKNOWN,
                                       side_source="project_party" if party else "model", by_user_id=by_user_id,
                                       alias_source="chat")
                if alias_key(raw) != alias_key(name):
                    add_alias(db, project_id, person.id, raw, source="chat", by_user_id=by_user_id)
                person.company_unconfirmed = party is not None
                person.needs_review = True
            out["created"] += 1
        except PeopleError:
            out["skipped"] += 1
    db.flush()
    return out


def _guest_company(db: Session, project_id: int, person: Person, row: Any, by_user_id: int | None) -> None:
    """Гость указал компанию из сторон проекта, а у человека компании ещё нет — проставить (сторона — от компании)."""
    if person.user_id is not None or (person.company_name or "").strip() or not (getattr(row, "company_name", None) or "").strip():
        return
    party = find_party(db, project_id, row.company_name)
    if party is not None:
        update_person(db, project_id, person.id, by_user_id=by_user_id, company_name=party.company_name)


# ---------------------------------------------------------------- удаление человека из проекта (ПФ8)

def forget_person(db: Session, project_id: int, person_id: int, *, by_user_id: int | None = None) -> dict[str, int]:
    """Полностью убрать человека из реестра проекта (вместе с теми, кого в него объединили): написания, голосовые слепки
    проекта, ссылки из встреч и вопросов, а в журнале правок — имена и данные «до / после». Остаётся запись «удалён» без имени.
    Учётка портала и общие голоса сотрудника не затрагиваются. Необратимо."""
    from app.app.models.decision_memory import DecisionEvent
    from app.app.models.meeting import MeetingParticipant
    from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile
    from app.app.models.speech_act import SpeechAct

    root = get(db, project_id, person_id)
    ids = {root.id}
    frontier = {root.id}
    while frontier:  # те, кого объединили с этим человеком (и кого объединили с ними)
        nxt = set(db.scalars(select(Person.id).where(Person.project_id == project_id, Person.merged_into.in_(frontier))).all()) - ids
        ids |= nxt
        frontier = nxt
    voices = list(db.scalars(select(SpeakerProfile).where(SpeakerProfile.person_id.in_(ids))).all())
    own = [v for v in voices if v.scope == "project" and v.project_id == project_id]
    own_ids = [v.id for v in own]
    if own_ids:
        for link in db.scalars(select(ArtifactSpeakerLink).where(ArtifactSpeakerLink.profile_id.in_(own_ids))).all():
            link.profile_id, link.status = None, "pending"
        for link in db.scalars(select(ArtifactSpeakerLink).where(ArtifactSpeakerLink.suggested_profile_id.in_(own_ids))).all():
            link.suggested_profile_id = None
    for v in voices:
        if v in own:
            db.delete(v)
        else:
            v.person_id = None
    for model in (MeetingParticipant, UnknownActor, SpeechAct):
        for row in db.scalars(select(model).where(model.person_id.in_(ids))).all():
            row.person_id = None
    lost_subjects = set()
    for event in db.scalars(select(DecisionEvent).where(DecisionEvent.actor_person_id.in_(ids))).all():
        event.actor_person_id = None
        lost_subjects.add(event.subject_id)
    if lost_subjects:
        db.flush()
        sync_decision_memory(db, project_id, person_ids=set(), subject_ids=lost_subjects)
    for entry in db.scalars(select(PeopleLog).where(PeopleLog.project_id == project_id, PeopleLog.person_id.in_(ids))).all():
        entry.person_id, entry.before_json, entry.after_json = None, None, None  # имя и данные — стереть, факт правки остаётся
    removed_aliases = db.query(PersonAlias).filter(PersonAlias.person_id.in_(ids)).delete(synchronize_session=False)
    db.query(Person).filter(Person.id.in_(ids)).update({Person.merged_into: None}, synchronize_session=False)
    db.flush()
    db.query(Person).filter(Person.id.in_(ids)).delete(synchronize_session=False)
    _log(db, project_id, None, by_user_id, "forget", None, {"people": len(ids), "aliases": removed_aliases, "voices": len(own_ids)})
    db.flush()
    return {"people": len(ids), "aliases": removed_aliases, "voices": len(own_ids)}
