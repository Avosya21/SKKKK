"""Подтверждение требований человеком (только admin): очередь, подтвердить / отклонить / сменить статус.

Требование контракта — это сущность (decision/task) проекта; решение человека записывается на неё:
- confirm: needs_review=False (в контракте verification=human_confirmed), в value_json → human_review {by, at, action};
- reject: сущность выключается мягко (is_active=False), из контракта исчезает (агент видит «removed» в get_changes), данные не удаляются;
- status: человек задаёт статус (proposed/agreed/agreed_out_of_scope/rejected/implemented), причина обязательна; статус получает status_basis=human_set.
Каждое действие пишется в журнал активности (кто, когда, что).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.services import contract as ctr
from app.app.services.auto_review import needs_human, public_model_review, undo_model_review
from app.app.services.user_activity import ACTION_CONFIRM, ACTION_REVIEW, BLOCK_KNOWLEDGE, log_activity

STATUSES = ("proposed", "agreed", "agreed_out_of_scope", "rejected", "implemented")
MAX_BATCH = 200


class ReviewError(ValueError):
    pass


def _model_rejected_rows(db: Session, project_id: int) -> list[dict[str, Any]]:
    """Требования, выключенные моделью (live) и не возвращённые человеком: их нет в контракте, но их можно «Вернуть»."""
    rows = db.execute(
        select(Entity, Artifact).join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.is_active.is_(False), Entity.entity_type.in_(("decision", "task")),
               Entity.value_json.like('%"model_review"%'))
        .order_by(Entity.id.desc())
    ).all()
    out = []
    for ent, art in rows:
        try:
            value = json.loads(ent.value_json or "{}")
        except ValueError:
            continue
        review = public_model_review(value) or {}
        if not review.get("deactivated") or review.get("undone"):
            continue
        out.append({"id": f"entity-{ent.id}", "source_entity_id": ent.id, "statement": str(value.get("text") or value.get("description") or ""),
                    "status": "rejected", "status_basis": "model_rejected", "verification": "model_rejected", "kind": ent.entity_type,
                    "estimate": None, "human_review": value.get("human_review"), "analysis": None, "model_review": review, "needs_human": False,
                    "quote": None, "speaker": value.get("speaker"), "side": None, "said_at": None, "artifact_id": art.artifact_id, "evidence_count": 0})
    return out


def review_list(db: Session, settings: Settings, project_id: int, *, status: str | None = None, basis: str | None = None,
                verification: str | None = None, side: str | None = None, q: str | None = None, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """Очередь требований. verification: human_confirmed | model_confirmed | extracted | needs_human (ждут человека) | model_rejected."""
    items = ctr.get_contract(db, settings, project_id).files["requirements"]["items"]
    rejected = _model_rejected_rows(db, project_id)
    counts = {
        "total": len(items),
        "unconfirmed": sum(1 for r in items if r["verification"] != "human_confirmed"),
        "needs_human": sum(1 for r in items if needs_human(r["verification"], r.get("model_review"))),
        "model_confirmed": sum(1 for r in items if r["verification"] == "model_confirmed"),
        "model_rejected": len(rejected),
    }
    if verification == "model_rejected":
        rows = [r for r in rejected if not q or q.lower() in r["statement"].lower()]
        return {"total": len(rows), "counts": counts, "items": rows[offset: offset + max(1, min(limit, 200))]}
    rows = []
    for r in items:
        ev = r.get("evidence") or []
        if status and r["status"] != status:
            continue
        if basis and r.get("status_basis") != basis:
            continue
        if verification == "needs_human":
            if not needs_human(r["verification"], r.get("model_review")):
                continue
        elif verification and r["verification"] != verification:
            continue
        if side and not any(e.get("side") == side for e in ev):
            continue
        if q and q.lower() not in r["statement"].lower():
            continue
        first = ev[0] if ev else {}
        rows.append({"id": r["id"], "source_entity_id": r.get("source_entity_id"), "statement": r["statement"], "status": r["status"],
                     "status_basis": r.get("status_basis"), "verification": r["verification"], "kind": r.get("kind"), "estimate": r.get("estimate"),
                     "human_review": r.get("human_review"), "analysis": r.get("analysis"), "model_review": r.get("model_review"),
                     "needs_human": needs_human(r["verification"], r.get("model_review")),
                     "quote": first.get("quote"), "speaker": first.get("speaker"), "side": first.get("side"),
                     "said_at": first.get("said_at"), "artifact_id": first.get("artifact_id"), "evidence_count": len(ev)})
    total = len(rows)
    return {"total": total, "counts": counts, "items": rows[offset: offset + max(1, min(limit, 200))]}


def undo_model(db: Session, settings: Settings, project_id: int, entity_id: int, *, user) -> dict[str, Any]:
    """«Вернуть»: отменить решение модели по сущности-требованию проекта (включить выключенное, записать решение человека)."""
    ent = db.get(Entity, entity_id)
    art = db.get(Artifact, ent.artifact_id) if ent else None
    if not ent or not art or art.project_id != project_id or ent.entity_type not in ("decision", "task"):
        raise ReviewError("Требование не найдено")
    try:
        review = undo_model_review(ent, user_name=user.name, user_id=user.id)
    except ValueError as exc:
        raise ReviewError(str(exc)) from exc
    if review.get("conflict_link"):
        # выключена как проигравшая в конфликте — снимаем и разрешение модели по этому конфликту
        from app.app.services.review_conflicts import release_conflict

        release_conflict(db, int(review["conflict_link"]), user_name=user.name)
    db.commit()
    ctr._cache.pop(project_id, None)
    log_activity(db, user_id=user.id, project_id=project_id, block=BLOCK_KNOWLEDGE, action=ACTION_REVIEW,
                 title=f"Отменено решение модели по сущности #{entity_id}", content=str(review.get("verdict") or ""),
                 target_kind="entity", target_id=entity_id)
    return {"entity_id": entity_id, "is_active": ent.is_active, "undone": True}


def _entity_for(db: Session, settings: Settings, project_id: int, requirement_id: str) -> Entity:
    for r in ctr.get_contract(db, settings, project_id).files["requirements"]["items"]:
        if r["id"] == requirement_id:
            ent = db.get(Entity, r["source_entity_id"])
            art = db.get(Artifact, ent.artifact_id) if ent else None
            if ent and art and art.project_id == project_id and ent.entity_type in ("decision", "task"):
                return ent
    raise ReviewError("Требование не найдено")


def apply(db: Session, settings: Settings, project_id: int, requirement_id: str, action: str, *, user, reason: str | None = None,
          status: str | None = None) -> dict[str, Any]:
    if action not in ("confirm", "reject", "status"):
        raise ReviewError("action: confirm | reject | status")
    ent = _entity_for(db, settings, project_id, requirement_id)
    try:
        value = json.loads(ent.value_json or "{}")
    except ValueError:
        value = {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    review = {"by": user.name, "user_id": user.id, "at": now, "action": action}
    if reason:
        review["reason"] = reason.strip()[:500]
    if action == "confirm":
        ent.needs_review = False
    elif action == "reject":
        ent.is_active = False
        ent.needs_review = False
    else:
        if status not in STATUSES:
            raise ReviewError(f"status: один из {', '.join(STATUSES)}")
        if not (reason or "").strip():
            raise ReviewError("Для смены статуса нужна причина")
        value["human_status"] = status
        ent.needs_review = False
        review["status"] = status
    value["human_review"] = review
    ent.value_json = json.dumps(value, ensure_ascii=False)
    db.commit()
    ctr._cache.pop(project_id, None)
    log_activity(db, user_id=user.id, project_id=project_id, block=BLOCK_KNOWLEDGE, action=ACTION_CONFIRM if action == "confirm" else ACTION_REVIEW,
                 title=f"Требование {requirement_id}: {action}", content=(reason or status or "")[:200], target_kind="requirement", target_id=requirement_id)
    return {"id": requirement_id, "action": action}


def apply_batch(db: Session, settings: Settings, project_id: int, requirement_ids: list[str], *, user) -> dict[str, Any]:
    if not requirement_ids or len(requirement_ids) > MAX_BATCH:
        raise ReviewError(f"Нужно от 1 до {MAX_BATCH} требований")
    done, failed = [], []
    for rid in requirement_ids:
        try:
            apply(db, settings, project_id, str(rid), "confirm", user=user)
            done.append(rid)
        except ReviewError as exc:
            failed.append({"id": rid, "why": str(exc)})
    return {"confirmed": done, "failed": failed}
