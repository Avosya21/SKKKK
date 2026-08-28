from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db, get_engine
from app.app.dependencies import get_current_user, require_role
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity, Link, NoteRef, Resolution, UserNote
from app.app.models.project import Project
from app.app.models.user import User
from app.app.services.chat import answer_chat, chat_is_pending, list_chat_messages
from app.app.services.entities import reextract_project_entities
from app.app.services.graph import build_graph_payload, entity_text
from app.app.services.ollama_client import embed_texts
from app.app.services.vector_index import init_vector_store, upsert_entity_vectors
from app.app.services.project_deadlines import list_project_deadlines
from app.app.services.project_tasks import (
    TASK_ENTITIES_REEXTRACT,
    TASK_TZ_REBUILD,
    TaskAlreadyRunning,
    list_active_tasks,
    track_project_task,
)
from app.app.services.storage import project_notes_dir
from app.app.services.tz import rebuild_tz, tz_paths

logger = logging.getLogger("skkkk.api.knowledge")
router = APIRouter(tags=["knowledge"])


class ChatRequest(BaseModel):
    project_id: int
    question: str = Field(min_length=1, max_length=4000)


class NoteRefIn(BaseModel):
    kind: str = Field(min_length=1, max_length=32)
    id: int


class NoteCreate(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    refs: list[NoteRefIn] = Field(default_factory=list)


class LinkCreate(BaseModel):
    project_id: int
    source_entity_id: int
    target_entity_id: int
    link_type: str
    description: str = Field(min_length=1)


class ResolveConflict(BaseModel):
    winning_entity_id: int
    reason: str = Field(min_length=1)


class EntityReview(BaseModel):
    action: str  # accept | reject | edit
    value_json: str | None = Field(default=None, max_length=32768)


@router.get("/projects/{project_id}/search")
def semantic_search(
    project_id: int,
    q: str,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
):
    from app.app.db import get_engine
    from app.app.services.ollama_client import embed_texts
    from app.app.services.retrieval import enrich_and_rerank_hits
    from app.app.services.vector_index import init_vector_store, search_vectors

    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    if settings.dry_run:
        return {"query": q, "results": []}
    init_vector_store(get_engine())
    vec = embed_texts(settings, [q])[0]
    hits = search_vectors(db, vec, project_id=project_id, limit=max(limit * 2, 10))
    hits = enrich_and_rerank_hits(db, hits, limit=limit)
    threshold = project.settings().get("search_min_score", settings.search_min_score)
    return {"query": q, "results": [h for h in hits if h["score"] >= threshold]}


@router.get("/projects/{project_id}/graph/data")
def project_graph(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
):
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    return build_graph_payload(db, project_id)


@router.get("/projects/{project_id}/artifacts/{artifact_db_id}/entities")
def list_entities(
    project_id: int,
    artifact_db_id: int,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
):
    art = db.get(Artifact, artifact_db_id)
    if not art or art.project_id != project_id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    rows = db.scalars(select(Entity).where(Entity.artifact_id == art.id)).all()
    return [
        {
            "id": e.id,
            "entity_type": e.entity_type,
            "value_json": e.value_json,
            "needs_review": e.needs_review,
            "is_active": e.is_active,
        }
        for e in rows
    ]


@router.post("/entities/{entity_id}/review")
def review_entity(
    entity_id: int,
    body: EntityReview,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _user: Annotated[User, Depends(get_current_user)],
):
    ent = db.get(Entity, entity_id)
    if not ent:
        raise HTTPException(status_code=404, detail="Entity not found")
    if body.action == "accept":
        ent.needs_review = False
    elif body.action == "reject":
        ent.is_active = False
        ent.needs_review = False
    elif body.action == "edit" and body.value_json:
        ent.value_json = body.value_json
        ent.needs_review = False
        if not settings.dry_run:
            init_vector_store(get_engine())
            vec = embed_texts(settings, [entity_text(ent.value_json)])[0]
            upsert_entity_vectors(db, [(ent.id, vec)])
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
    db.commit()
    return {"ok": True}


@router.post("/projects/{project_id}/notes")
def create_note(
    project_id: int,
    body: NoteCreate,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    notes_dir = project_notes_dir(settings, project_id)
    notes_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    fname = f"user_note_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_{user.id}.txt"
    notes_root = notes_dir.resolve()
    fpath = (notes_dir / fname).resolve()
    if fpath.parent != notes_root:
        raise HTTPException(status_code=400, detail="Invalid note path")
    fpath.write_text(body.text, encoding="utf-8")
    note = UserNote(project_id=project_id, user_id=user.id, text=body.text, file_path=str(fpath))
    db.add(note)
    db.flush()
    for ref in body.refs:
        db.add(
            NoteRef(
                note_id=note.id,
                target_kind=ref.kind,
                target_id=ref.id,
            )
        )
    db.commit()
    return {"id": note.id, "file_path": str(fpath)}


@router.post("/links")
def create_link(
    body: LinkCreate,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = db.get(Project, body.project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    src = db.get(Entity, body.source_entity_id)
    tgt = db.get(Entity, body.target_entity_id)
    if not src or not tgt:
        raise HTTPException(status_code=404, detail="Entity not found")
    src_art = db.get(Artifact, src.artifact_id)
    tgt_art = db.get(Artifact, tgt.artifact_id)
    if (
        not src_art
        or not tgt_art
        or src_art.project_id != body.project_id
        or tgt_art.project_id != body.project_id
    ):
        raise HTTPException(status_code=404, detail="Entity not found")
    link = Link(
        project_id=body.project_id,
        source_entity_id=body.source_entity_id,
        target_entity_id=body.target_entity_id,
        link_type=body.link_type,
        is_automatic=False,
        created_by=user.id,
        description=body.description,
    )
    db.add(link)
    db.commit()
    return {"id": link.id}


@router.get("/projects/{project_id}/conflicts")
def list_conflicts(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role("admin"))],
):
    links = db.scalars(
        select(Link).where(Link.project_id == project_id, Link.link_type == "POTENTIAL_CONFLICT")
    ).all()
    out = []
    for lnk in links:
        if db.scalar(select(Resolution.id).where(Resolution.link_id == lnk.id)):
            continue
        src = db.get(Entity, lnk.source_entity_id)
        tgt = db.get(Entity, lnk.target_entity_id)
        out.append(
            {
                "link_id": lnk.id,
                "source": {"id": src.id, "type": src.entity_type, "value": src.value_json} if src else None,
                "target": {"id": tgt.id, "type": tgt.entity_type, "value": tgt.value_json} if tgt else None,
                "score": lnk.similarity_score,
            }
        )
    return out


@router.post("/conflicts/{link_id}/resolve")
def resolve_conflict(
    link_id: int,
    body: ResolveConflict,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_role("admin"))],
):
    link = db.get(Link, link_id)
    if not link or link.link_type != "POTENTIAL_CONFLICT":
        raise HTTPException(status_code=404, detail="Conflict not found")
    if body.winning_entity_id not in {link.source_entity_id, link.target_entity_id}:
        raise HTTPException(status_code=400, detail="winner must be source or target")
    losing = (
        link.target_entity_id
        if body.winning_entity_id == link.source_entity_id
        else link.source_entity_id
    )
    loser = db.get(Entity, losing)
    if loser:
        loser.is_active = False
    db.add(
        Resolution(
            link_id=link.id,
            winning_entity_id=body.winning_entity_id,
            losing_entity_id=losing,
            reason=body.reason,
            resolved_by=admin.id,
            status="resolved",
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="already resolved") from exc
    return {"ok": True}


@router.get("/projects/{project_id}/tasks/active")
def project_active_tasks(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
):
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"tasks": list_active_tasks(db, project_id=project_id)}


@router.post("/projects/{project_id}/rebuild-tz")
def rebuild_tz_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    admin: Annotated[User, Depends(require_role("admin"))],
):
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        with track_project_task(
            db,
            task_type=TASK_TZ_REBUILD,
            project_id=project_id,
            user_id=admin.id,
        ):
            path = rebuild_tz(db, settings, project_id, manual=True)
    except TaskAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"path": str(path)}


@router.get("/projects/{project_id}/deadlines")
def project_deadlines(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
):
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    return list_project_deadlines(db, project_id)


@router.post("/projects/{project_id}/reextract-entities")
def reextract_entities_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        with track_project_task(
            db,
            task_type=TASK_ENTITIES_REEXTRACT,
            project_id=project_id,
            user_id=user.id,
        ):
            return reextract_project_entities(db, settings, project_id)
    except TaskAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/projects/{project_id}/tz/content")
def get_tz(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _user: Annotated[User, Depends(get_current_user)],
):
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    current, _ = tz_paths(settings, project_id)
    if not current.is_file():
        return {"content": "", "path": str(current)}
    from app.app.services.plain_text import clean_summary_output
    from app.app.services.tz import _artifact_filename_map, _humanize_artifact_refs

    raw = current.read_text(encoding="utf-8")
    body = clean_summary_output(raw)
    body = _humanize_artifact_refs(body, _artifact_filename_map(db, project_id))
    return {
        "content": body,
        "path": str(current),
    }


@router.get("/projects/{project_id}/tz/versions")
def tz_versions(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _user: Annotated[User, Depends(get_current_user)],
):
    from app.app.models.entity import Changelog

    rows = db.scalars(
        select(Changelog).where(Changelog.project_id == project_id).order_by(Changelog.id.desc())
    ).all()
    _, versions_dir = tz_paths(settings, project_id)
    files = sorted(versions_dir.glob("current_tz_*.md"), reverse=True)
    return {
        "changelog": [
            {
                "id": r.id,
                "version": r.version_label,
                "summary": r.summary_of_changes,
                "tz_path": r.tz_path,
            }
            for r in rows
        ],
        "files": [f.name for f in files],
    }


@router.get("/projects/{project_id}/chat/history")
def chat_history(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"messages": list_chat_messages(db, project_id=project_id, user_id=user.id)}


@router.get("/projects/{project_id}/chat/status")
def chat_status(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    pending = chat_is_pending(db, project_id=project_id, user_id=user.id)
    return {"pending": pending, "label": "ИИ готовит ответ…" if pending else ""}


@router.post("/chat")
def chat_endpoint(
    body: ChatRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = db.get(Project, body.project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    if chat_is_pending(db, project_id=body.project_id, user_id=user.id):
        raise HTTPException(status_code=409, detail="ИИ уже готовит ответ на предыдущий вопрос")
    from app.app.llm_settings import uses_local_gemma
    from app.app.models.job import Job

    if uses_local_gemma(settings.llm_provider):
        running = db.scalar(select(func.count()).select_from(Job).where(Job.status == "running")) or 0
        if running:
            raise HTTPException(status_code=409, detail="GPU занят расшифровкой")
    try:
        return answer_chat(db, settings, body.project_id, user.id, body.question)
    except TaskAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
