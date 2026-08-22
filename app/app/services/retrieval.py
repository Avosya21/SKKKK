from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact


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
