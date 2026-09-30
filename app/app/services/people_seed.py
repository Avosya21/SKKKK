"""Первичное наполнение реестра людей проекта (docs/plans/PLATFORM-FOUNDATION-PLAN.md, ПФ2).

Источники имён: сотрудники проекта с карточкой (``card``), явный список сторон в настройках проекта
(``project_party``), участники и роли из извлечения, авторы переписки, спикеры созвонов (``model``).
Один человек в разных написаниях склеивается только там, где это безопасно: то же имя, «Фамилия Имя» наоборот,
одиночное имя, которое подходит ровно одному полному имени. Остальное остаётся отдельными людьми или вопросами
«Кто это?» — окончательно решает человек. Сторону даёт самый сильный источник: карточка, затем список проекта,
затем перевес ролей в извлечении.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.models.person import SIDE_CUSTOMER, SIDE_EXECUTOR, SIDE_UNKNOWN, Person
from app.app.services import people
from app.app.services.people import alias_key, side_from_contract

SEED_SOURCE_RANK = {"model": 0, "project_party": 1, "card": 2}


@dataclass
class Mention:
    name: str
    source: str  # card | project_party | model
    side: str | None = None  # executor | customer | None
    user_id: int | None = None
    company_name: str | None = None
    job_title: str | None = None
    where: str = ""  # откуда взято: participant | chat | speaker | settings | card


@dataclass
class Cluster:
    names: list[str] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        # Написание без скобок, кириллическое, самое полное
        plain = [n for n in self.names if "(" not in n] or self.names
        return sorted(plain, key=lambda n: (not any("а" <= ch.lower() <= "я" for ch in n), -len(n.split()), -len(n)))[0]


def _tokens(name: str) -> list[str]:
    return alias_key(name).split()


PLACEHOLDER_RE = re.compile(r"^(speaker|спикер|голос)[\s_-]*\d*$", re.IGNORECASE)
# Слова, которые описывают роль или источник звука, а не имя человека
ROLE_WORDS = {
    "директор", "руководитель", "менеджер", "бухгалтер", "бухгалтерия", "заказчик", "исполнитель", "клиент", "участник",
    "аналитик", "разработчик", "фоновый голос", "голос", "диктор", "неизвестный", "неизвестно", "unknown", "экран", "речь на экране",
}


def _is_description(text: str) -> bool:
    """Скобки с компанией, должностью или аббревиатурой — не второе имя человека."""
    t = text.strip()
    return bool(t) and ("," in t or any(ch.isdigit() for ch in t) or (t.isupper() and len(t) <= 4) or t.casefold() in ROLE_WORDS)


def clean_name(raw: str | None) -> str | None:
    """Имя человека из строки материала или None, если это метка спикера, роль или звук.
    «speaker_4 (Анна)» -> «Анна»; «Анна (Компания, должность)» -> «Анна»; «speaker_2», «Директор (Компания)» -> None."""
    text = " ".join((raw or "").split())
    if not text:
        return None
    outer, sep, rest = text.partition("(")
    outer = outer.strip()
    inner = rest.partition(")")[0].strip() if sep else ""
    if PLACEHOLDER_RE.match(outer):
        return clean_name(inner) if inner and not _is_description(inner) else None
    if not outer or outer.casefold() in ROLE_WORDS:
        return None
    if sep and _is_description(inner):
        return outer
    return text


def cluster_mentions(mentions: list[Mention]) -> tuple[list[Cluster], list[Mention]]:
    """Склеить написания одного человека. Возвращает (кластеры, неоднозначные одиночные имена)."""
    by_key: dict[str, Cluster] = {}
    cleaned: list[Mention] = []
    for m in mentions:  # метки спикеров, роли и «фоновый голос» людьми не становятся; описание в скобках отбрасывается
        name = clean_name(m.name)
        if name:
            cleaned.append(replace(m, name=name))
    mentions = cleaned
    for m in mentions:
        key = alias_key(m.name)
        if not key:
            continue
        by_key.setdefault(key, Cluster()).mentions.append(m)
        if m.name not in by_key[key].names:
            by_key[key].names.append(m.name)
    keys = list(by_key)
    # «Фамилия Имя» = «Имя Фамилия»
    parent: dict[str, str] = {k: k for k in keys}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    # «Илья Эталон (Ilya Abramov)» — одно написание двух имён одного человека: склеиваем то и другое
    for m in mentions:
        if "(" in m.name and ")" in m.name:
            outer, _, rest = m.name.partition("(")
            inner = rest.partition(")")[0]
            parts = [alias_key(outer), alias_key(inner)]
            for part in parts:
                if part in parent and part != alias_key(m.name):
                    parent[find(part)] = find(alias_key(m.name))
    two = defaultdict(list)
    for k in keys:
        t = k.split()
        if len(t) == 2:
            two[frozenset(t)].append(k)
    for group in two.values():
        for other in group[1:]:
            parent[find(other)] = find(group[0])
    # «Ирина В.» = «Ирина Васильева», если такая ровно одна
    for k in keys:
        t = k.split()
        if len(t) == 2 and len(t[1]) == 1:
            owners = {find(f) for f in keys if len(f.split()) == 2 and f.split()[0] == t[0] and f.split()[1].startswith(t[1]) and f != k}
            if len(owners) == 1:
                parent[find(k)] = next(iter(owners))
    # одиночное имя -> ровно одно полное имя, начинающееся с него
    ambiguous: list[Mention] = []
    full = [k for k in keys if len(k.split()) >= 2]
    for k in keys:
        if len(k.split()) != 1:
            continue
        owners = {find(f) for f in full if f.split()[0] == k or k in f.split()}
        if len(owners) == 1:
            parent[find(k)] = next(iter(owners))
        elif len(owners) > 1:
            ambiguous.extend(by_key[k].mentions)
    merged: dict[str, Cluster] = {}
    ambiguous_keys = {alias_key(m.name) for m in ambiguous}
    for k in keys:
        if k in ambiguous_keys:
            continue  # одиночное имя, подходящее нескольким людям: не склеиваем, спросим человека
        root = find(k)
        target = merged.setdefault(root, Cluster())
        for n in by_key[k].names:
            if n not in target.names:
                target.names.append(n)
        target.mentions.extend(by_key[k].mentions)
    return list(merged.values()), ambiguous


def cluster_side(cluster: Cluster) -> tuple[str, str]:
    """(сторона, источник): сильнейший источник; внутри модели — перевес; ничья — unknown."""
    best_rank, sides = -1, Counter()
    for m in cluster.mentions:
        if not m.side:
            continue
        rank = SEED_SOURCE_RANK.get(m.source, 0)
        if rank > best_rank:
            best_rank, sides = rank, Counter()
        if rank == best_rank:
            sides[m.side] += 1
    if not sides:
        return SIDE_UNKNOWN, "model"
    (top, n), *rest = sides.most_common()
    if rest and rest[0][1] == n:
        return SIDE_UNKNOWN, "model"
    source = {2: "card", 1: "project_party", 0: "model"}[best_rank]
    return top, source


def _mentioned_names(db: Session, settings: Settings, project_id: int) -> set[str]:
    """Имена из участников извлечения, авторов переписки и спикеров созвонов проекта (без сторон и без учёток)."""
    import json

    from app.app.services.chat_export import artifact_messages_path, load_export
    from app.app.services.transcript_turns import load_turns

    names: set[str] = set()
    for ent in db.scalars(select(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
                          .where(Artifact.project_id == project_id, Entity.entity_type == "participant", Entity.is_active.is_(True))).all():
        try:
            n = str(json.loads(ent.value_json or "{}").get("name") or "").strip()
        except ValueError:
            continue
        if n:
            names.add(n)
    for art in db.scalars(select(Artifact).where(Artifact.project_id == project_id)).all():
        if art.file_type != "audio" and art.source_type == "document":
            export = load_export(artifact_messages_path(settings, art))
            if export:
                names |= {m["author"] for m in export["messages"] if m.get("author")}
                continue
        if art.source_type == "call":
            try:
                names |= {t.known_speaker for t in load_turns(settings, art) if getattr(t, "known_speaker", None)}
            except Exception:  # noqa: BLE001 - нет стенограммы: материал пропускаем
                continue
    return {cleaned for n in names if (cleaned := clean_name(n))}


def collect_mentions(db: Session, settings: Settings, project_id: int) -> list[Mention]:
    from app.app.models.company import Company
    from app.app.models.project import Project
    from app.app.models.project_member import ProjectMember
    from app.app.services import agent_settings, contract_text as ct
    from app.app.services import user_profile as profiles

    out: list[Mention] = []
    project = db.get(Project, project_id)
    # 1. сотрудники проекта с карточкой
    for member in db.scalars(select(ProjectMember).where(ProjectMember.project_id == project_id)).all():
        card = profiles.get_profile(db, member.user_id)
        if card is None or not (card.last_name or card.first_name):
            continue
        company = db.get(Company, card.company_id) if card.company_id else None
        side = people.party_side(db, project_id, company.name if company else None) or (company.side if company and company.side else None)
        for name in (f"{card.first_name} {card.last_name}".strip(), f"{card.last_name} {card.first_name}".strip()):
            out.append(Mention(name, "card", side or None, member.user_id, company.name if company else None, card.job_title, "card"))
    # 1б. любой сотрудник портала с карточкой, чьё имя точно совпало с написанием из материалов (админ видит все проекты и
    # не числится участником; тёзки — две учётки с одним именем — не сопоставляются)
    known = {m.user_id for m in out if m.user_id}
    by_name: dict[str, list[tuple[Any, Any]]] = defaultdict(list)
    for card in db.scalars(select(profiles.UserProfile)).all():
        if card.user_id in known or not (card.last_name and card.first_name):
            continue
        company = db.get(Company, card.company_id) if card.company_id else None
        for variant in (f"{card.first_name} {card.last_name}", f"{card.last_name} {card.first_name}"):
            by_name[alias_key(variant)].append((card, company))
    mentioned = _mentioned_names(db, settings, project_id)
    for name in mentioned:
        owners = {c.user_id: (c, co) for c, co in by_name.get(alias_key(name), [])}
        if len(owners) != 1:
            continue
        card, company = next(iter(owners.values()))
        side = people.party_side(db, project_id, company.name if company else None) or (company.side if company and company.side else None)
        for variant in (f"{card.first_name} {card.last_name}", f"{card.last_name} {card.first_name}"):
            out.append(Mention(variant, "card", side or None, card.user_id, company.name if company else None, card.job_title, "card"))
    # 2. явный список сторон в настройках проекта
    cfg = agent_settings.get_settings_for(project) if project is not None else {}
    for row in cfg.get("participants") or []:
        name, side = str(row.get("name", "")).strip(), side_from_contract(row.get("side"))
        if name:
            out.append(Mention(name, "project_party", None if side == SIDE_UNKNOWN else side, where="settings"))
    # 3. участники и роли из извлечения
    hints = cfg.get("side_hints")
    for ent in db.scalars(select(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
                          .where(Artifact.project_id == project_id, Entity.entity_type == "participant", Entity.is_active.is_(True))).all():
        import json

        try:
            value = json.loads(ent.value_json or "{}")
        except ValueError:
            continue
        name = str(value.get("name") or "").strip()
        if name:
            role = value.get("role")
            out.append(Mention(name, "model", side_from_contract(ct.side_from_role(role, hints)) if role else None, where="participant"))
    # 4. авторы переписки и 5. спикеры созвонов
    from app.app.services.chat_export import artifact_messages_path, load_export
    from app.app.services.transcript_turns import load_turns

    for art in db.scalars(select(Artifact).where(Artifact.project_id == project_id)).all():
        if art.file_type != "audio" and art.source_type == "document":
            export = load_export(artifact_messages_path(settings, art))
            if export:
                for author in {m["author"] for m in export["messages"] if m.get("author")}:
                    out.append(Mention(author, "model", None, where="chat"))
                continue
        if art.source_type == "call":
            try:
                turns = load_turns(settings, art)
            except Exception:  # noqa: BLE001 - нет стенограммы: пропускаем материал, отчёт покажет остальное
                continue
            for name in {t.known_speaker for t in turns if getattr(t, "known_speaker", None)}:
                out.append(Mention(name, "model", None, where="speaker"))
    return out


def prune_placeholders(db: Session, project_id: int, *, by_user_id: int | None = None) -> dict[str, int]:
    """Убрать из реестра то, что людьми не является: метки спикеров, роли, «фоновый голос» (их занесла прежняя версия
    наполнения). Удаляются только записи, которых человек не трогал: без учётки, без голосов и участий в встречах,
    сторона — догадка. Метки спикеров, прописанные как написания у настоящих людей, снимаются: в разных встречах
    ``speaker_4`` — разные люди."""
    from sqlalchemy import delete, update

    from app.app.models.meeting import MeetingParticipant
    from app.app.models.person import PeopleLog, PersonAlias, UnknownActor
    from app.app.models.speaker_profile import SpeakerProfile

    removed = aliases = 0
    for person in people.list_people(db, project_id, include_merged=True):
        if clean_name(person.display_name) is not None:
            continue
        touched = (
            person.user_id is not None or person.side_source != "model" or person.voice_consent_at is not None
            or db.scalar(select(SpeakerProfile.id).where(SpeakerProfile.person_id == person.id)) is not None
            or db.scalar(select(MeetingParticipant.id).where(MeetingParticipant.person_id == person.id)) is not None
            or db.scalar(select(Person.id).where(Person.merged_into == person.id)) is not None
        )
        if touched:
            continue
        before = {"display_name": person.display_name, "side": person.side}
        db.execute(delete(PersonAlias).where(PersonAlias.person_id == person.id))
        for row in db.scalars(select(UnknownActor).where(UnknownActor.person_id == person.id)).all():
            row.person_id = None
        db.execute(update(PeopleLog).where(PeopleLog.person_id == person.id).values(person_id=None))  # история остаётся без ссылки
        people._log(db, project_id, None, by_user_id, "prune", before, None)
        db.delete(person)
        removed += 1
    merged = renamed = 0
    for person in people.list_people(db, project_id):  # имя с описанием в скобках («Марина (Компания, бухгалтерия)») -> просто имя
        cleaned = clean_name(person.display_name)
        if not cleaned or cleaned == person.display_name or person.user_id is not None or person.side_source != "model":
            continue
        target = _same_person_target(db, project_id, cleaned, exclude=person.id)
        if target is not None:
            people.merge(db, project_id, target.id, person.id, by_user_id=by_user_id)
            merged += 1
        else:
            people.update_person(db, project_id, person.id, display_name=cleaned, by_user_id=by_user_id)
            renamed += 1
    for alias in db.scalars(select(PersonAlias).where(PersonAlias.project_id == project_id)).all():
        person = db.get(Person, alias.person_id)
        if person is not None and alias_key(alias.alias) != alias_key(person.display_name) and PLACEHOLDER_RE.match(alias.alias.partition("(")[0].strip()):
            db.delete(alias)
            aliases += 1
    db.flush()
    return {"removed": removed, "aliases": aliases, "merged": merged, "renamed": renamed}


def _same_person_target(db: Session, project_id: int, name: str, *, exclude: int) -> Person | None:
    """Уже существующий человек с этим именем (точное написание или «Имя О.» -> единственный «Имя Ов…»)."""
    hit = people.resolve(db, project_id, name, allow_first_name=False)
    if hit is not None and hit.person.id != exclude:
        return hit.person
    tokens = alias_key(name).split()
    if len(tokens) == 2 and len(tokens[1]) == 1:
        found = {}
        for p in people.list_people(db, project_id):
            parts = alias_key(p.display_name).split()
            if p.id != exclude and len(parts) == 2 and parts[0] == tokens[0] and parts[1].startswith(tokens[1]):
                found[p.id] = p
        if len(found) == 1:
            return next(iter(found.values()))
    return None


def seed_project(db: Session, settings: Settings, project_id: int, *, apply: bool = False, by_user_id: int | None = None) -> dict[str, Any]:
    """Черновик реестра. ``apply=False`` — только отчёт; ``True`` — записать людей, алиасы и вопросы «Кто это?»."""
    mentions = collect_mentions(db, settings, project_id)
    clusters, ambiguous = cluster_mentions(mentions)
    report: dict[str, Any] = {"people": [], "unknown": [], "created": 0, "skipped_existing": 0}
    if apply:
        report["pruned"] = prune_placeholders(db, project_id, by_user_id=by_user_id)
    for cluster in sorted(clusters, key=lambda c: c.display_name):
        side, side_source = cluster_side(cluster)
        card = next((m for m in cluster.mentions if m.source == "card"), None)
        row = {"name": cluster.display_name, "aliases": cluster.names, "side": side, "side_source": side_source,
               "user_id": card.user_id if card else None, "company": card.company_name if card else None,
               "seen_in": sorted({m.where for m in cluster.mentions if m.where})}
        report["people"].append(row)
        if not apply:
            continue
        existing = next((h.person for n in cluster.names if (h := people.resolve(db, project_id, n, allow_first_name=False))), None)
        if existing is not None:
            report["skipped_existing"] += 1
            if card is not None and card.user_id and existing.user_id is None and not people.user_taken(db, project_id, card.user_id):
                people.update_person(db, project_id, existing.id, by_user_id=by_user_id, user_id=card.user_id,
                                     **({"company_name": card.company_name} if card.company_name and not existing.company_name else {}),
                                     **({"job_title": card.job_title} if card.job_title and not existing.job_title else {}))
                if side != SIDE_UNKNOWN:
                    people.set_side(db, project_id, existing.id, side, source=side_source, by_user_id=by_user_id)
            for n in cluster.names:
                try:
                    people.add_alias(db, project_id, existing.id, n, source="model", by_user_id=by_user_id)
                except people.PeopleError:
                    pass
            continue
        person = people.create_person(db, project_id, cluster.display_name, user_id=row["user_id"], company_name=row["company"],
                                      job_title=card.job_title if card else None, side=side, side_source=side_source,
                                      by_user_id=by_user_id, alias_source="card" if card else "model")
        for n in cluster.names:
            if alias_key(n) != alias_key(cluster.display_name):
                try:
                    people.add_alias(db, project_id, person.id, n, source="model", by_user_id=by_user_id)
                except people.PeopleError:
                    pass
        report["created"] += 1
    for m in ambiguous:
        candidates = [c.display_name for c in clusters if m.name and alias_key(m.name) in " ".join(alias_key(n) for n in c.names).split()]
        report["unknown"].append({"label": m.name, "where": m.where, "candidates": candidates})
        if apply and people.resolve(db, project_id, m.name, allow_first_name=False) is None:
            kind = "chat_author" if m.where == "chat" else "speaker_label"
            people.note_unknown(db, project_id, kind, m.name, suggestions=[{"name": n} for n in candidates])
    if apply:
        report["companies_normalized"] = people.normalize_project_companies(db, project_id)
        db.commit()
    return report
