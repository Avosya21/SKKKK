"""Первичное наполнение реестра людей проекта (docs/plans/PLATFORM-FOUNDATION-PLAN.md, ПФ2).

Источники имён: сотрудники проекта с карточкой (``card``), явный список сторон в настройках проекта
(``project_party``), участники и роли из извлечения, авторы переписки, спикеры созвонов (``model``).
Один человек в разных написаниях склеивается только там, где это безопасно: то же имя, «Фамилия Имя» наоборот,
одиночное имя, которое подходит ровно одному полному имени. Остальное остаётся отдельными людьми или вопросами
«Кто это?» — окончательно решает человек. Сторону даёт самый сильный источник: карточка, затем список проекта,
затем перевес ролей в извлечении.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.models.person import SIDE_CUSTOMER, SIDE_EXECUTOR, SIDE_UNKNOWN
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


def cluster_mentions(mentions: list[Mention]) -> tuple[list[Cluster], list[Mention]]:
    """Склеить написания одного человека. Возвращает (кластеры, неоднозначные одиночные имена)."""
    by_key: dict[str, Cluster] = {}
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


def seed_project(db: Session, settings: Settings, project_id: int, *, apply: bool = False, by_user_id: int | None = None) -> dict[str, Any]:
    """Черновик реестра. ``apply=False`` — только отчёт; ``True`` — записать людей, алиасы и вопросы «Кто это?»."""
    mentions = collect_mentions(db, settings, project_id)
    clusters, ambiguous = cluster_mentions(mentions)
    report: dict[str, Any] = {"people": [], "unknown": [], "created": 0, "skipped_existing": 0}
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
        db.commit()
    return report
