"""Противоречия проекта для LLM чата и ТЗ — хронологией, человеческими строками (CONFLICT-AUTONOMY-PLAN этап 5, Р4).

Раньше чат и ТЗ получали первые 20 конфликтов проекта сырым ``value_json`` без отбора по теме и без «что в силе».
Теперь — только конфликты по найденным материалам/сущностям, решённые — «В силе: … Раньше: … Основание: …»,
открытые — «Не решено: A ↔ B» с датами, чтобы модель назвала обе версии, а не выбирала молча.
Конфликты, которые модель или правило признали «не конфликтом», не выводятся — это шум.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact
from app.app.models.entity import Entity, Link, Resolution

CONFLICT = "POTENTIAL_CONFLICT"
MATERIAL_TYPES = frozenset({"decision", "task", "fact"})


def _line(db: Session, ent: Entity) -> str:
    from app.app.services.retrieval import format_entity_line

    return format_entity_line(ent.entity_type, ent.value_json, db.get(Artifact, ent.artifact_id))


def _basis(res: Resolution) -> str:
    if res.resolved_by is not None:
        return f"решил человек — {res.reason}"
    return res.reason  # у авторешений причина уже начинается с «правило …:» или «модель …:»


def conflict_lines(
    db: Session,
    project_id: int,
    *,
    artifact_ids: set[int] | None = None,
    entity_ids: set[int] | None = None,
    limit: int = 20,
) -> tuple[list[str], list[str]]:
    """(решённые, открытые) строки. Без фильтров — весь проект (ТЗ); с фильтрами — только касающиеся их (чат)."""
    q = select(Link).where(Link.project_id == project_id, Link.link_type == CONFLICT)
    filters = []
    if entity_ids:
        filters += [Link.source_entity_id.in_(entity_ids), Link.target_entity_id.in_(entity_ids)]
    if artifact_ids:
        ids = select(Entity.id).where(Entity.artifact_id.in_(artifact_ids))
        filters += [Link.source_entity_id.in_(ids), Link.target_entity_id.in_(ids)]
    if artifact_ids is not None or entity_ids is not None:
        if not filters:
            return [], []
        q = q.where(or_(*filters))
    resolved: list[str] = []
    open_: list[str] = []
    for link in db.scalars(q.order_by(Link.id.desc())).all():
        if len(resolved) + len(open_) >= limit:
            break
        src, tgt = db.get(Entity, link.source_entity_id), db.get(Entity, link.target_entity_id)
        if src is None or tgt is None or src.entity_type not in MATERIAL_TYPES or tgt.entity_type not in MATERIAL_TYPES:
            continue
        res = db.scalar(select(Resolution).where(Resolution.link_id == link.id))
        if res is not None and res.status == "model_dismissed":
            continue
        if res is not None:
            win = db.get(Entity, res.winning_entity_id)
            lose = db.get(Entity, res.losing_entity_id)
            if win is None or lose is None:
                continue
            resolved.append(f"- В силе: {_line(db, win)}\n  Раньше считалось (заменено): {_line(db, lose)}\n  Основание: {_basis(res)}")
        elif src.is_active and tgt.is_active:
            open_.append(f"- Не решено, назови обе версии с датами:\n  1) {_line(db, src)}\n  2) {_line(db, tgt)}")
    return resolved, open_
