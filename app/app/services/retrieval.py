from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.services.graph import _cosine, entity_text
from app.app.services.vector_index import load_entity_vectors

MATERIAL_ENTITY_TYPES = frozenset({"decision", "task", "fact"})
ENTITY_SEARCH_LIMIT = 12


def search_entities(
    db: Session,
    query_vec: list[float],
    *,
    project_id: int,
    limit: int = ENTITY_SEARCH_LIMIT,
    min_score: float = 0.35,
) -> list[dict[str, Any]]:
    """Ближайшие decision/task/fact проекта по уже посчитанному вектору вопроса."""
    rows = db.execute(
        select(Entity, Artifact)
        .join(Artifact, Artifact.id == Entity.artifact_id)
        .where(
            Artifact.project_id == project_id,
            Entity.is_active.is_(True),
            Entity.entity_type.in_(MATERIAL_ENTITY_TYPES),
        )
        .order_by(Entity.id.asc())
    ).all()
    if not rows:
        return []
    vectors = load_entity_vectors(db, [ent.id for ent, _art in rows])
    ranked: list[tuple[float, Entity, Artifact]] = []
    for ent, art in rows:
        vec = vectors.get(ent.id)
        if not vec:
            continue
        score = _cosine(query_vec, vec)
        if score < min_score:
            continue
        ranked.append((score, ent, art))
    ranked.sort(key=lambda item: (-item[0], -item[1].id))
    out: list[dict[str, Any]] = []
    for score, ent, art in ranked[:limit]:
        out.append(
            {
                "entity_id": ent.id,
                "entity_type": ent.entity_type,
                "value_json": ent.value_json,
                "text": entity_text(ent.value_json),
                "score": score,
                "artifact_db_id": art.id,
                "artifact_id": art.artifact_id,
                "filename": art.original_filename,
                "priority_weight": art.priority_weight,
            }
        )
    return out


def weighted_score(semantic_score: float, priority_weight: float) -> float:
    """Семантика × (0.5 + priority_weight): созвон 0.5 → ×1.0, документ 0.8 → ×1.3."""
    return float(semantic_score) * (0.5 + float(priority_weight))


def enrich_and_rerank_hits(
    db: Session,
    hits: list[dict[str, Any]],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if not hits:
        return []
    artifact_ids = {h["artifact_id"] for h in hits}
    artifacts = {
        a.id: a
        for a in db.scalars(select(Artifact).where(Artifact.id.in_(artifact_ids))).all()
    }
    enriched: list[dict[str, Any]] = []
    for hit in hits:
        art = artifacts.get(hit["artifact_id"])
        weight = art.priority_weight if art else 0.5
        row = dict(hit)
        row["priority_weight"] = weight
        row["weighted_score"] = weighted_score(hit["score"], weight)
        if art:
            row["source_type"] = art.source_type
            row["filename"] = art.original_filename
        enriched.append(row)
    enriched.sort(key=lambda row: row["weighted_score"], reverse=True)
    if limit is not None:
        return enriched[:limit]
    return enriched


def artifact_sort_key(artifact: Artifact) -> tuple[float, float, int]:
    upload_ts = 0.0
    if artifact.upload_date:
        upload_ts = artifact.upload_date.timestamp()
    return (-artifact.priority_weight, -upload_ts, -artifact.id)
