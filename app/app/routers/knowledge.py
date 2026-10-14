"""Знания проекта: поиск по смыслу, граф, сущности, заметки и связи, конфликты, ТЗ и его версии, чат, дедлайны."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db, get_engine
from app.app.dependencies import get_current_user, require_role
from app.app.services.project_access import require_project, require_project_staff
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity, Link, NoteRef, Resolution, UserNote
from app.app.models.project import Project
from app.app.models.user import User
from app.app.services.chat import ChatAlreadyPending, answer_chat, chat_is_pending, chat_owner, list_chat_messages
from app.app.services.llm_lanes import chat_status as chat_queue_status
from app.app.services.graph import build_graph_payload, entity_text
from app.app.services.ollama_client import embed_texts
from app.app.services.vector_index import init_vector_store, upsert_entity_vectors
from app.app.services.project_deadlines import list_project_deadlines
from app.app.services.project_tasks import (
    TaskAlreadyRunning,
    list_active_tasks,
)
from app.app.services.queue import (
    PROJECT_TASK_CARD,
    PROJECT_TASK_ENTITIES,
    PROJECT_TASK_TZ,
    ProjectTaskRefused,
    queue_project_button,
)
from app.app.services.storage import project_notes_dir
from app.app.services.tz import tz_paths
from app.app.services.user_activity import (
    ACTION_NOTE,
    ACTION_RESOLVE,
    ACTION_REVIEW,
    BLOCK_KNOWLEDGE,
    log_activity,
    list_activity,
)

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
    user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
):
    from app.app.db import get_engine
    from app.app.services.ollama_client import embed_texts
    from app.app.services.chunk_fts import init_chunk_fts
    from app.app.services.retrieval import fuse_chunk_hits
    from app.app.services.vector_index import init_vector_store, search_vectors

    project = require_project(db, user, project_id)
    if settings.dry_run:
        return {"query": q, "results": []}
    init_vector_store(get_engine())
    init_chunk_fts(get_engine())
    vec = embed_texts(settings, [q])[0]
    hits = search_vectors(db, vec, project_id=project_id, limit=max(limit * 2, 10))
    threshold = project.settings().get("search_min_score", settings.search_min_score)
    hits = fuse_chunk_hits(db, q, vec, hits, project_id=project_id, limit=limit, min_score=threshold)
    return {"query": q, "results": hits}


@router.get("/projects/{project_id}/graph/data")
def project_graph(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    require_project(db, user, project_id)
    return build_graph_payload(db, project_id)


@router.get("/projects/{project_id}/artifacts/{artifact_db_id}/entities")
def list_entities(
    project_id: int,
    artifact_db_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    require_project(db, user, project_id)
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


def _keep_provenance(old_json: str, new_json: str) -> str:
    """Правка текста сущности не стирает «кто / когда / откуда»: ключи провенанса переносятся из старого значения."""
    from app.app.services.graph import PROVENANCE_KEYS

    try:
        old, new = json.loads(old_json), json.loads(new_json)
    except (json.JSONDecodeError, TypeError):
        return new_json
    if not isinstance(old, dict) or not isinstance(new, dict):
        return new_json
    merged = dict(new)
    for key in PROVENANCE_KEYS:
        if key in old and key not in merged:
            merged[key] = old[key]
    return json.dumps(merged, ensure_ascii=False)


@router.post("/entities/{entity_id}/review")
def review_entity(
    entity_id: int,
    body: EntityReview,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    ent = db.get(Entity, entity_id)
    if not ent:
        raise HTTPException(status_code=404, detail="Entity not found")
    art = db.get(Artifact, ent.artifact_id)
    if not art:
        raise HTTPException(status_code=404, detail="Entity not found")
    require_project(db, user, art.project_id)
    if body.action == "accept":
        ent.needs_review = False
    elif body.action == "reject":
        ent.is_active = False
        ent.needs_review = False
    elif body.action == "edit" and body.value_json:
        ent.value_json = _keep_provenance(ent.value_json, body.value_json)
        ent.needs_review = False
        if not settings.dry_run:
            init_vector_store(get_engine())
            vec = embed_texts(settings, [entity_text(ent.value_json)])[0]
            upsert_entity_vectors(db, [(ent.id, vec)])
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
    db.commit()
    log_activity(
        db,
        user_id=user.id,
        project_id=art.project_id,
        block=BLOCK_KNOWLEDGE,
        action=ACTION_REVIEW,
        title="Проверил сущность",
        content=body.action,
        target_kind="entity",
        target_id=entity_id,
    )
    return {"ok": True}


@router.post("/projects/{project_id}/notes")
def create_note(
    project_id: int,
    body: NoteCreate,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project(db, user, project_id)
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
    log_activity(
        db,
        user_id=user.id,
        project_id=project_id,
        block=BLOCK_KNOWLEDGE,
        action=ACTION_NOTE,
        title="Добавил заметку",
        content=body.text[:500],
        target_kind="note",
        target_id=note.id,
    )
    return {"id": note.id, "file_path": str(fpath)}


@router.post("/links")
def create_link(
    body: LinkCreate,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project(db, user, body.project_id)
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
    admin: Annotated[User, Depends(require_role("admin"))],
    view: str = "pending",
):
    """Конфликты проекта: pending — ждут человека (с вердиктом модели, если он теневой/неуверенный); model — решены моделью;
    important — сводка: авторешения по деньгам и срокам и выключения более нового пункта в пользу более раннего, ещё не
    отменённые (CONFLICT-AUTONOMY-PLAN этап 6; решение владельца 2026-09-28)."""
    from app.app.services.review_conflicts import conflict_rows

    require_project(db, admin, project_id)
    if view not in ("pending", "model", "important"):
        raise HTTPException(status_code=400, detail="Неизвестный вид списка конфликтов: нужен pending, model или important")
    return conflict_rows(db, project_id, view=view)


@router.post("/conflicts/{link_id}/undo-model")
def undo_conflict_model(
    link_id: int,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_role("admin"))],
):
    """«Вернуть»: отменить решение локальной модели по конфликту (снять разрешение, включить проигравшую сторону)."""
    from app.app.services.review_conflicts import undo_conflict

    link = db.get(Link, link_id)
    if not link or link.link_type != "POTENTIAL_CONFLICT":
        raise HTTPException(status_code=404, detail="Conflict not found")
    require_project(db, admin, link.project_id)
    try:
        result = undo_conflict(db, link_id, user_name=admin.name, user_id=admin.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    log_activity(db, user_id=admin.id, project_id=link.project_id, block=BLOCK_KNOWLEDGE, action=ACTION_RESOLVE,
                 title="Отменил решение модели по конфликту", content="", target_kind="link", target_id=link_id)
    return result


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
    log_activity(
        db,
        user_id=admin.id,
        project_id=link.project_id,
        block=BLOCK_KNOWLEDGE,
        action=ACTION_RESOLVE,
        title="Разрешил конфликт",
        content=body.reason,
        target_kind="link",
        target_id=link_id,
    )
    return {"ok": True}


class ProjectCardIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


@router.get("/projects/{project_id}/card")
def get_project_card(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Карточка проекта — контекст для извлечения требований (REQUIREMENTS-PLAN этап 3): цель, системы, этапы, участники, термины."""
    from app.app.services.extraction_context import read_project_card

    require_project(db, user, project_id)
    text, meta = read_project_card(settings, project_id)
    return {"text": text, "meta": meta, "can_edit": user.role == "admin"}


@router.put("/projects/{project_id}/card")
def put_project_card(
    project_id: int,
    body: ProjectCardIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    admin: Annotated[User, Depends(require_role("admin"))],
):
    """Правка администратора: главнее черновика модели, пересборка черновика её не перетирает."""
    from app.app.services.extraction_context import save_project_card

    require_project(db, admin, project_id)
    meta = save_project_card(settings, project_id, body.text, source="human", by=admin.name)
    log_activity(db, user_id=admin.id, project_id=project_id, block=BLOCK_KNOWLEDGE, action=ACTION_REVIEW,
                 title="Изменил карточку проекта", content=body.text[:200], target_kind="project", target_id=project_id)
    return {"text": body.text, "meta": meta}


@router.post("/projects/{project_id}/card/rebuild")
def rebuild_project_card(
    project_id: int,
    body: dict,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    admin: Annotated[User, Depends(require_role("admin"))],
):
    """Новый черновик карточки моделью — задачей очереди воркера. Правка человека заменяется только по явному
    ``discard_human``."""
    from app.app.services.extraction_context import read_project_card

    require_project(db, admin, project_id)
    _text, meta = read_project_card(settings, project_id)
    if meta.get("source") == "human" and not body.get("discard_human"):
        raise HTTPException(status_code=409, detail="Карточку правил человек: черновик модели заменит правку. Подтвердите замену.")
    return _queue_button(db, settings, project_id=project_id, task=PROJECT_TASK_CARD, user_id=admin.id)


def _queue_button(db: Session, settings: Settings, **kwargs) -> dict:
    """Кнопка, которой нужна модель, — задача воркера (LLM-LANES-PLAN этап 3): у API модель только для чата."""
    try:
        return queue_project_button(db, settings.data_dir, **kwargs)
    except ProjectTaskRefused as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.get("/projects/{project_id}/open-questions")
def project_open_questions(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Вопросы к заказчику: варианты без выбора и вопросы без ответа из материалов (REQUIREMENTS-PLAN А7)."""
    from app.app.services import contract as ctr
    from app.app.services.topics import topic_labels

    require_project(db, user, project_id)
    items = ctr.get_contract(db, settings, project_id).files["open_questions"]["items"]
    labels = topic_labels(db, {q.get("topic_id") for q in items if q.get("topic_id")})
    return {"total": len(items), "items": [{**q, "topic": labels.get(q.get("topic_id"))} for q in items]}


@router.get("/projects/{project_id}/tasks/active")
def project_active_tasks(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    require_project(db, user, project_id)
    return {"tasks": list_active_tasks(db, project_id=project_id)}


@router.post("/projects/{project_id}/rebuild-tz")
def rebuild_tz_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    admin: Annotated[User, Depends(require_role("admin"))],
):
    require_project(db, admin, project_id)
    return _queue_button(db, settings, project_id=project_id, task=PROJECT_TASK_TZ, user_id=admin.id)


@router.get("/projects/{project_id}/deadlines")
def project_deadlines(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    require_project(db, user, project_id)
    return list_project_deadlines(db, project_id)


@router.get("/projects/{project_id}/digest")
def project_digest(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    """«Что нового с прошлого захода» (Ш5): строки полоски; страница зовёт после тоста «файл обработан»."""
    from app.app.services.project_access import staff_ui_role
    from app.app.services.project_digest import digest_for_page

    project = require_project(db, user, project_id)
    return digest_for_page(db, settings, project, user, staff_ui_role(db, user, project))


@router.post("/projects/{project_id}/digest/dismiss")
def project_digest_dismiss(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """«Скрыть» полоску до следующего обработанного материала (Ш5)."""
    from app.app.services.project_digest import dismiss

    require_project(db, user, project_id)
    dismiss(db, user.id, project_id)
    return {"dismissed": True}


@router.get("/projects/{project_id}/tasks")
def project_tasks_list(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    view: str = "open", assignee: str | None = None, offset: int = 0, limit: int = 50,
):
    """Карточка «Задачи» (Ш4): все задачи проекта — извлечённые с правками и ручные; вкладки и фильтр по ответственному."""
    from app.app.services import task_board as tb
    from app.app.services.project_access import staff_ui_role

    project = require_project(db, user, project_id)
    try:
        result = tb.list_project_tasks(db, project_id, view=view, assignee=assignee, offset=max(0, offset), limit=limit)
    except tb.TaskError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return {**result, "can_edit": staff_ui_role(db, user, project) in ("admin", "manager")}


def _task_log(db: Session, user: User, project_id: int, title: str, target: str, content: str = "") -> None:
    from app.app.services.user_activity import ACTION_TASK_EDIT

    log_activity(db, user_id=user.id, project_id=project_id, block=BLOCK_KNOWLEDGE, action=ACTION_TASK_EDIT,
                 title=title[:200], content=content[:200], target_kind="task", target_id=target)


@router.patch("/projects/{project_id}/tasks/{entity_id}")
def project_task_edit(
    project_id: int, entity_id: int, body: dict,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Правка извлечённой задачи: статус, ответственный, срок — admin и manager проекта; поверх извлечённого (Ш4)."""
    from app.app.services import task_board as tb

    require_project_staff(db, user, project_id)
    try:
        out = tb.edit_extracted(db, project_id, entity_id, body, user=user)
    except tb.TaskError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    _task_log(db, user, project_id, f"Задача e{entity_id}: " + ", ".join(sorted(k for k in body if k in tb.EDIT_FIELDS)),
              f"e{entity_id}", json.dumps({k: body[k] for k in body if k in tb.EDIT_FIELDS}, ensure_ascii=False))
    return out


@router.post("/projects/{project_id}/tasks")
def project_task_create(
    project_id: int, body: dict,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Ручная задача (Ш4): admin и manager проекта."""
    from app.app.services import task_board as tb

    require_project_staff(db, user, project_id)
    try:
        out = tb.create_manual(db, project_id, body, user=user)
    except tb.TaskError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    _task_log(db, user, project_id, "Добавлена задача", out["id"], str(body.get("description") or ""))
    return out


@router.patch("/projects/{project_id}/manual-tasks/{task_id}")
def project_manual_task_edit(
    project_id: int, task_id: int, body: dict,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    from app.app.services import task_board as tb

    require_project_staff(db, user, project_id)
    try:
        out = tb.edit_manual(db, project_id, task_id, body)
    except tb.TaskError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    _task_log(db, user, project_id, f"Задача m{task_id} изменена", f"m{task_id}", json.dumps(body, ensure_ascii=False))
    return out


@router.delete("/projects/{project_id}/manual-tasks/{task_id}")
def project_manual_task_delete(
    project_id: int, task_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Убрать ручную задачу (мягко: строка остаётся с is_active=0)."""
    from app.app.services import task_board as tb

    require_project_staff(db, user, project_id)
    try:
        out = tb.delete_manual(db, project_id, task_id)
    except tb.TaskError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    _task_log(db, user, project_id, f"Задача m{task_id} удалена", f"m{task_id}")
    return out


@router.post("/projects/{project_id}/reextract-entities")
def reextract_entities_endpoint(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project_staff(db, user, project_id)
    return _queue_button(db, settings, project_id=project_id, task=PROJECT_TASK_ENTITIES, user_id=user.id, empty_ok=True)


@router.get("/projects/{project_id}/tz/content")
def get_tz(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    require_project(db, user, project_id)
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
    user: Annotated[User, Depends(get_current_user)],
):
    from app.app.models.entity import Changelog

    require_project(db, user, project_id)
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


@router.get("/projects/{project_id}/activity")
def project_activity(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    all_users: Annotated[bool, Query()] = False,
):
    project = require_project(db, user, project_id)
    scope_all = bool(all_users) and user.role == "admin"
    events = list_activity(
        db,
        project_id=project_id,
        user_id=None if scope_all else user.id,
    )
    return {"events": events, "all_users": scope_all}


@router.get("/projects/{project_id}/chat/history")
def chat_history(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project(db, user, project_id)
    return {"messages": list_chat_messages(db, project_id=project_id, user_id=user.id)}


@router.get("/projects/{project_id}/chat/status")
def chat_status(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project(db, user, project_id)
    pending = chat_is_pending(db, project_id=project_id, user_id=user.id)
    queue = chat_queue_status(chat_owner(project_id, user.id))
    label = queue.get("label") or ("ИИ готовит ответ…" if pending else "")
    return {"pending": pending, "label": label, "position": queue.get("position")}


@router.post("/chat")
def chat_endpoint(
    body: ChatRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project(db, user, body.project_id)
    # Чат не блокируется обработкой (DECISIONS 2026-10-05, LLM-LANES-PLAN): модель работает в два потока,
    # вопрос ждёт только своей очереди в полосе чата и загрузки модели.
    try:
        return answer_chat(db, settings, body.project_id, user.id, body.question)
    except (TaskAlreadyRunning, ChatAlreadyPending) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
