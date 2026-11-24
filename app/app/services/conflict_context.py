"""Противоречия для чата и резюме: хронология, без служебных фактов Jira; для резюме — короткий человеческий вид.

Раньше чат и ТЗ получали первые 20 конфликтов проекта сырым ``value_json`` без отбора по теме и без «что в силе».
Теперь — только конфликты по найденным материалам/сущностям, решённые — «В силе: … Раньше: … Основание: …»,
открытые — «Не решено: …» с датами, чтобы модель назвала обе версии, а не выбирала молча.
Конфликты, которые модель или правило признали «не конфликтом», не выводятся — это шум.
Служебные факты вроде ``updated_at`` / ``last_update_time`` из выгрузок Jira в строки не попадают:
это метаданные носителя, а не содержание проекта. Для читаемого резюме (``audience=human``) —
короткая строка без тегов ``[fact]`` и без имён файлов.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact
from app.app.models.entity import Entity, Link, Resolution

CONFLICT = "POTENTIAL_CONFLICT"
MATERIAL_TYPES = frozenset({"decision", "task", "fact"})
Audience = Literal["llm", "human"]

# Ключи fact из служебных полей Jira/носителя: модель часто выписывает их из шапки задачи,
# граф склеивает похожие метки времени разных тикетов в «конфликт», резюме заливается шумом.
_NOISE_FACT_KEYS = frozenset(
    {
        "updated_at",
        "last_update_time",
        "last_updated",
        "created_at",
        "created",
        "updated",
        "lastviewed",
        "last_viewed",
        "id",
        "issuekey",
        "issue_key",
        "watches",
        "votes",
        "workratio",
        "progress",
        "aggregateprogress",
        "timeoriginalestimate",
        "timeestimate",
        "timespent",
        "aggregatetimeoriginalestimate",
        "aggregatetimeestimate",
        "aggregatetimespent",
        "statuscategorychangedate",
        "thumbnail",
        "rank",
        "ранг",
    }
)
_ISO_TS = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)


def normalize_fact_key(key: str) -> str:
    return str(key or "").strip().casefold().replace("-", "_").replace(" ", "_")


def is_noise_fact_key(key: str) -> bool:
    """Служебный ключ fact (метаданные Jira/носителя), не содержание проекта."""
    return normalize_fact_key(key) in _NOISE_FACT_KEYS


def is_noise_fact_entity(ent: Entity) -> bool:
    if ent.entity_type != "fact":
        return False
    try:
        value = json.loads(ent.value_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(value, dict):
        return False
    key = normalize_fact_key(str(value.get("key") or ""))
    if key in _NOISE_FACT_KEYS:
        return True
    # «что-то_at / *_time» с одним ISO-временем — тоже метка носителя, не решение проекта.
    raw = str(value.get("value") or "").strip()
    if key.endswith(("_at", "_time", "_date")) and _ISO_TS.match(raw):
        return True
    return False


def open_question_lines(db: Session, project_id: int, *, limit: int = 30) -> list[str]:
    """Вопросы к заказчику и варианты без выбора (REQUIREMENTS-PLAN А7) — для раздела ТЗ «Открытые вопросы и риски»."""
    rows = db.scalars(
        select(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.entity_type == "open_question", Entity.is_active.is_(True))
        .order_by(Entity.id.desc()).limit(limit)
    ).all()
    return [f"- {_line(db, ent)}" for ent in rows]


def _line(db: Session, ent: Entity) -> str:
    from app.app.services.retrieval import format_entity_line

    return format_entity_line(ent.entity_type, ent.value_json, db.get(Artifact, ent.artifact_id))


def _short_body(ent: Entity) -> str:
    try:
        value = json.loads(ent.value_json or "{}")
    except (json.JSONDecodeError, TypeError):
        value = {}
    if not isinstance(value, dict):
        return str(ent.value_json or "").strip() or "—"
    if ent.entity_type == "fact" and "key" in value:
        return f"{value.get('key')}: {value.get('value')}"
    body = str(value.get("text") or value.get("description") or "").strip()
    return body or "—"


def _human_side(db: Session, ent: Entity) -> str:
    from app.app.services.recorded_at import material_date_label

    body = _short_body(ent)
    art = db.get(Artifact, ent.artifact_id)
    label = material_date_label(art) if art is not None else ""
    return f"{body} ({label})" if label else body


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
    audience: Audience = "llm",
    material_types: frozenset[str] | set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """(решённые, открытые) строки. Без фильтров — весь проект (ТЗ); с фильтрами — только касающиеся их (чат).

    ``audience=llm`` — полный ``format_entity_line`` для чата и материала сборки.
    ``audience=human`` — короткая строка для читаемого резюме (без тегов типа и имён файлов).
    ``material_types`` — какие типы сторон учитывать (по умолчанию decision/task/fact).
    """
    allowed = frozenset(material_types) if material_types is not None else MATERIAL_TYPES
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
        if src is None or tgt is None or src.entity_type not in allowed or tgt.entity_type not in allowed:
            continue
        if is_noise_fact_entity(src) or is_noise_fact_entity(tgt):
            continue
        res = db.scalar(select(Resolution).where(Resolution.link_id == link.id))
        if res is not None and res.status == "model_dismissed":
            continue
        if res is not None:
            win = db.get(Entity, res.winning_entity_id)
            lose = db.get(Entity, res.losing_entity_id)
            if win is None or lose is None:
                continue
            if is_noise_fact_entity(win) or is_noise_fact_entity(lose):
                continue
            if audience == "human":
                resolved.append(
                    f"- В силе: {_human_side(db, win)}. Раньше: {_human_side(db, lose)}."
                )
            else:
                resolved.append(
                    f"- В силе: {_line(db, win)}\n  Раньше считалось (заменено): {_line(db, lose)}\n  Основание: {_basis(res)}"
                )
        elif src.is_active and tgt.is_active:
            if audience == "human":
                open_.append(f"- Не решено: {_human_side(db, src)} ↔ {_human_side(db, tgt)}")
            else:
                # Без «назови обе версии»: это подсказка модели, она утекала в резюме как текст для человека.
                open_.append(f"- Не решено:\n  1) {_line(db, src)}\n  2) {_line(db, tgt)}")
    return resolved, open_
