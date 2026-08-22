from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.db import get_engine
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity, Link, Resolution, UserNote
from app.app.models.project import Project
from app.app.services.graph import build_graph_payload
from app.app.services.ollama_client import embed_texts
from app.app.services.storage import parsed_dir, project_root
from app.app.services.tz import tz_paths
from app.app.services.vector_index import init_vector_store, search_vectors


def _artifact_row(art: Artifact) -> dict[str, Any]:
    return {
        "artifact_id": art.artifact_id,
        "filename": art.original_filename,
        "file_type": art.file_type,
        "source_type": art.source_type,
        "status": art.status,
        "transcript_url": art.transcript_html_url,
        "created_at": art.created_at.isoformat() if art.created_at else None,
    }


def mcp_project_overview(db: Session, settings: Settings, project_id: int) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise ValueError("Project not found")
    counts = dict(
        db.execute(
            select(Artifact.status, func.count())
            .where(Artifact.project_id == project_id)
            .group_by(Artifact.status)
        ).all()
    )
    done = int(counts.get("done", 0))
    total = sum(int(v) for v in counts.values())
    entities = db.scalar(
        select(func.count())
        .select_from(Entity)
        .join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.is_active.is_(True))
    ) or 0
    from app.app.models.entity import Chunk

    chunks = db.scalar(select(func.count()).select_from(Chunk).where(Chunk.project_id == project_id)) or 0
    tz_path, _ = tz_paths(settings, project_id)
    return {
        "project_id": project_id,
        "name": project.name,
        "description": project.description,
        "artifacts_total": total,
        "artifacts_done": done,
        "entities": entities,
        "indexed_chunks": chunks,
        "has_project_summary": tz_path.is_file() and tz_path.stat().st_size > 0,
    }


def mcp_list_artifacts(
    db: Session,
    project_id: int,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    q = select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.id.desc()).limit(limit)
    if status:
        q = q.where(Artifact.status == status)
    return [_artifact_row(a) for a in db.scalars(q).all()]


def mcp_get_project_summary(db: Session, settings: Settings, project_id: int) -> dict[str, Any]:
    path, _ = tz_paths(settings, project_id)
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {"project_id": project_id, "path": str(path.relative_to(settings.data_dir)), "content": content}


def mcp_search_project(
    db: Session,
    settings: Settings,
    project_id: int,
    *,
    query: str,
    limit: int = 12,
) -> dict[str, Any]:
    if settings.dry_run:
        return {"query": query, "results": []}
    init_vector_store(get_engine())
    vectors = embed_texts(settings, [query])
    if not vectors:
        return {"query": query, "results": []}
    hits = search_vectors(db, vectors[0], project_id=project_id, limit=limit)
    results: list[dict[str, Any]] = []
    for hit in hits:
        if hit.get("score", 0) < settings.search_min_score:
            continue
        art = db.get(Artifact, hit.get("artifact_id"))
        results.append(
            {
                "score": hit.get("score"),
                "chunk_id": hit.get("chunk_id"),
                "artifact_id": art.artifact_id if art else None,
                "filename": art.original_filename if art else None,
                "text": hit.get("text", "")[:2000],
            }
        )
    return {"query": query, "results": results}


def mcp_list_entities(
    db: Session,
    project_id: int,
    *,
    entity_type: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    q = (
        select(Entity)
        .join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.is_active.is_(True))
        .order_by(Entity.id.desc())
        .limit(limit)
    )
    if entity_type:
        q = q.where(Entity.entity_type == entity_type)
    out: list[dict[str, Any]] = []
    for ent in db.scalars(q).all():
        art = db.get(Artifact, ent.artifact_id)
        try:
            value = json.loads(ent.value_json)
        except json.JSONDecodeError:
            value = ent.value_json
        out.append(
            {
                "id": ent.id,
                "entity_type": ent.entity_type,
                "value": value,
                "artifact_id": art.artifact_id if art else None,
                "filename": art.original_filename if art else None,
            }
        )
    return out


def mcp_get_knowledge_graph(db: Session, project_id: int, *, limit: int = 200) -> dict[str, Any]:
    return build_graph_payload(db, project_id, limit=limit)


def mcp_list_notes(db: Session, project_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(UserNote).where(UserNote.project_id == project_id).order_by(UserNote.id.desc()).limit(limit)
    ).all()
    return [
        {
            "id": n.id,
            "text": n.text,
            "author_id": n.user_id,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in rows
    ]


def mcp_list_conflicts(db: Session, project_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
    links = db.scalars(
        select(Link).where(Link.project_id == project_id, Link.link_type == "POTENTIAL_CONFLICT").limit(limit)
    ).all()
    out: list[dict[str, Any]] = []
    for lnk in links:
        src = db.get(Entity, lnk.source_entity_id)
        tgt = db.get(Entity, lnk.target_entity_id)
        res = db.scalar(select(Resolution).where(Resolution.link_id == lnk.id))
        out.append(
            {
                "link_id": lnk.id,
                "source_entity_id": lnk.source_entity_id,
                "target_entity_id": lnk.target_entity_id,
                "source": src.value_json if src else None,
                "target": tgt.value_json if tgt else None,
                "resolved": res is not None,
                "resolution": res.reason if res else None,
            }
        )
    return out


def _read_text(path: Path, max_chars: int = 120_000) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:max_chars]


def mcp_get_artifact_content(
    db: Session,
    settings: Settings,
    project_id: int,
    *,
    artifact_id: str,
    kind: str = "auto",
) -> dict[str, Any]:
    art = db.scalar(
        select(Artifact).where(Artifact.project_id == project_id, Artifact.artifact_id == artifact_id)
    )
    if not art:
        raise ValueError("Artifact not found")
    parsed = parsed_dir(settings, project_id, art.source_type)
    base = art.artifact_id
    candidates: dict[str, Path] = {
        "transcript": parsed / f"{base}_final.txt",
        "summary": parsed / f"{base}_call_summary.md",
        "entities": project_root(settings, project_id) / "structured" / f"{base}_entities.json",
    }
    if kind == "auto":
        order = ("summary", "transcript", "entities")
    else:
        order = (kind,)
    content = ""
    used_kind = kind
    for key in order:
        path = candidates.get(key)
        if path and path.is_file():
            content = _read_text(path)
            used_kind = key
            break
    return {
        "artifact_id": artifact_id,
        "filename": art.original_filename,
        "file_type": art.file_type,
        "status": art.status,
        "kind": used_kind,
        "content": content,
    }
