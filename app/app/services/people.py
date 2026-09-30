"""Реестр людей проекта (docs/plans/PLATFORM-FOUNDATION-PLAN.md, ПФ2).

Извлечение и контракт называют человека строкой («Мария», «Ilya Abramov»). Реестр превращает строку в человека:
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
    """Название компании человека к виду общего списка: «intervolga» и «INTERVOLGA» — одна компания.
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
    return row


def resolve(db: Session, project_id: int, label: str | None, *, allow_first_name: bool = True) -> Match | None:
    """Человек по написанию. Точный алиас — надёжно; «Мария» вместо «Мария Попова» — только если такое имя одно."""
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
    allowed = {"company_name", "company_id", "job_title", "authority", "display_name", "user_id"}
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
    return person


def merge(db: Session, project_id: int, keep_id: int, drop_id: int, *, by_user_id: int | None = None) -> Person:
    """«Это один человек»: алиасы, голоса, участия и вопросы «Кто это?» переходят к ``keep``; ``drop`` остаётся с ``merged_into``."""
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
    return keep


# ---------------------------------------------------------------- стороны проекта

def party_side(db: Session, project_id: int, company_name: str | None) -> str | None:
    """Сторона компании в этом проекте (Д1): ``executor`` / ``customer`` или None, если компания не задана."""
    key = alias_key(company_name)
    if not key:
        return None
    row = db.scalar(select(ProjectParty).where(ProjectParty.project_id == project_id, ProjectParty.company_key == key))
    return row.side if row is not None else None


def set_party(db: Session, project_id: int, company_name: str, side: str, *, company_id: int | None = None, by_user_id: int | None = None) -> ProjectParty:
    if side not in (SIDE_EXECUTOR, SIDE_CUSTOMER):
        raise PeopleError("Сторона компании: исполнитель или заказчик")
    key = alias_key(company_name)
    if not key:
        raise PeopleError("Пустое название компании")
    row = db.scalar(select(ProjectParty).where(ProjectParty.project_id == project_id, ProjectParty.company_key == key))
    before = None if row is None else {"company": row.company_name, "side": row.side}
    if row is None:
        row = ProjectParty(project_id=project_id, company_id=company_id, company_name=" ".join(company_name.split()), company_key=key, side=side)
        db.add(row)
    else:
        row.side, row.company_id = side, company_id or row.company_id
    _log(db, project_id, None, by_user_id, "set_party", before, {"company": row.company_name, "side": side})
    db.flush()
    return row


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
            add_alias(db, project_id, person.id, row.label, source="chat" if row.kind == "chat_author" else "human", by_user_id=by_user_id)
        row.person_id, row.status = person.id, "resolved"
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
    # «Мария» вместо «Мария Попова»: первое слово годится, только если у всех людей с таким первым словом одна сторона
    firsts: dict[str, set[str]] = {}
    for name, side in out.items():
        if " " in name:
            firsts.setdefault(name.split()[0], set()).add(side)
    for word, sides in firsts.items():
        if len(sides) == 1 and word not in out:
            out[word] = next(iter(sides))
    return out


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
