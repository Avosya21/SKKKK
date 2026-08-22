from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.db import get_engine
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity, Link, Resolution
from app.app.services.ollama_client import embed_texts
from app.app.services.vector_index import init_vector_store

logger = logging.getLogger("skkkk.graph")

CANCEL_PATTERNS = re.compile(
    r"(отменяем|вместо\s+пункта|исправляем|отменяем\s+прежн)",
    re.I,
)


def _entity_text(entity: Entity) -> str:
    try:
        data = json.loads(entity.value_json)
    except json.JSONDecodeError:
        return entity.value_json
    if isinstance(data, dict):
        return " ".join(str(v) for v in data.values())
    return str(data)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def seed_graph(db: Session, settings: Settings, artifact: Artifact, source_text: str) -> None:
    init_vector_store(get_engine())
    threshold = artifact.project.settings().get("similarity_threshold", settings.similarity_threshold)
    new_entities = db.scalars(select(Entity).where(Entity.artifact_id == artifact.id, Entity.is_active.is_(True))).all()
    if not new_entities:
        log_event(logger, "graph.skipped", artifact=artifact.artifact_id, reason="no active entities")
        return

    texts = [_entity_text(e) for e in new_entities]
    new_vecs = embed_texts(settings, texts) if texts else []

    prior = db.scalars(
        select(Entity)
        .join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == artifact.project_id, Entity.artifact_id != artifact.id, Entity.is_active.is_(True))
    ).all()
    prior_texts = [_entity_text(e) for e in prior]
    prior_vecs = embed_texts(settings, prior_texts) if prior_texts else []

    created: dict[str, int] = {}
    for ent, vec in zip(new_entities, new_vecs, strict=True):
        for old, old_vec in zip(prior, prior_vecs, strict=True):
            score = _cosine(vec, old_vec)
            if score < threshold:
                continue
            same_type = ent.entity_type == old.entity_type
            same_value = _entity_text(ent).strip().lower() == _entity_text(old).strip().lower()
            if same_type and not same_value:
                link_type = "POTENTIAL_CONFLICT"
            else:
                link_type = "SEMANTICALLY_RELATED"
            exists = db.scalar(
                select(Link.id).where(
                    Link.project_id == artifact.project_id,
                    Link.source_entity_id == ent.id,
                    Link.target_entity_id == old.id,
                    Link.link_type == link_type,
                )
            )
            if exists:
                continue
            db.add(
                Link(
                    project_id=artifact.project_id,
                    source_entity_id=ent.id,
                    target_entity_id=old.id,
                    link_type=link_type,
                    similarity_score=score,
                    is_automatic=True,
                    description=None,
                )
            )
            created[link_type] = created.get(link_type, 0) + 1

    log_event(
        logger,
        "graph.seeded",
        artifact=artifact.artifact_id,
        entities=len(new_entities),
        compared_with=len(prior),
        related=created.get("SEMANTICALLY_RELATED", 0),
        conflicts=created.get("POTENTIAL_CONFLICT", 0),
        threshold=round(threshold, 2),
    )
    # Сессия работает с autoflush=False, поэтому только что добавленные связи
    # не попадут в выборку авторазрешения без явного flush.
    db.flush()
    _try_auto_resolve(db, artifact, source_text)


def _try_auto_resolve(db: Session, artifact: Artifact, source_text: str) -> None:
    if not CANCEL_PATTERNS.search(source_text):
        return
    resolved = 0
    conflicts = db.scalars(
        select(Link).where(
            Link.project_id == artifact.project_id,
            Link.link_type == "POTENTIAL_CONFLICT",
            Link.is_automatic.is_(True),
        )
    ).all()
    for link in conflicts:
        if db.scalar(select(Resolution.id).where(Resolution.link_id == link.id)):
            continue
        newer = link.source_entity_id
        older = link.target_entity_id
        old_ent = db.get(Entity, older)
        if old_ent:
            old_ent.is_active = False
        db.add(
            Resolution(
                link_id=link.id,
                winning_entity_id=newer,
                losing_entity_id=older,
                reason="auto: explicit cancellation phrase in newer artifact",
                resolved_by=None,
                status="resolved",
            )
        )
        resolved += 1

    if resolved:
        log_event(
            logger,
            "graph.conflicts.auto_resolved",
            artifact=artifact.artifact_id,
            resolved=resolved,
        )


def build_graph_payload(db: Session, project_id: int, *, limit: int = 200) -> dict[str, Any]:
    artifacts = db.scalars(
        select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.id.desc()).limit(limit)
    ).all()
    entities = db.scalars(
        select(Entity)
        .join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.is_active.is_(True))
        .limit(limit)
    ).all()
    links = db.scalars(select(Link).where(Link.project_id == project_id).limit(limit * 2)).all()

    nodes: list[dict[str, Any]] = []
    for art in artifacts:
        nodes.append(
            {
                "id": f"artifact-{art.id}",
                "kind": "artifact",
                "label": art.original_filename,
                "artifact_id": art.artifact_id,
            }
        )
    for ent in entities:
        nodes.append(
            {
                "id": f"entity-{ent.id}",
                "kind": "entity",
                "label": f"{ent.entity_type}",
                "entity_type": ent.entity_type,
                "needs_review": ent.needs_review,
            }
        )
    edges = [
        {
            "id": f"link-{lnk.id}",
            "source": f"entity-{lnk.source_entity_id}",
            "target": f"entity-{lnk.target_entity_id}",
            "type": lnk.link_type,
            "score": lnk.similarity_score,
            "automatic": lnk.is_automatic,
        }
        for lnk in links
    ]
    for ent in entities:
        edges.append(
            {
                "id": f"contains-{ent.id}",
                "source": f"artifact-{ent.artifact_id}",
                "target": f"entity-{ent.id}",
                "type": "CONTAINS",
                "automatic": True,
            }
        )
    return {"nodes": nodes[:limit], "edges": edges[: limit * 2]}
