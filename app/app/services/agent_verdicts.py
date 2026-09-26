"""Вердикты внешнего агента (analysis_verdict): список, подтверждение и отклонение человеком (только admin).

Подтверждение: needs_review=False -> в контракте analysis.human_confirmed=true (verified_by по-прежнему machine_code: это разные вещи).
Отклонение: мягкое (is_active=False), запись остаётся в базе. Кто и когда пишется в value_json.human_review и в журнал активности."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.services import contract as ctr
from app.app.services.user_activity import ACTION_CONFIRM, ACTION_REVIEW, BLOCK_KNOWLEDGE, log_activity


class VerdictError(ValueError):
    pass


def _rows(db: Session, project_id: int):
    return db.execute(select(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
                      .where(Artifact.project_id == project_id, Entity.entity_type == "analysis_verdict").order_by(Entity.id.desc())).scalars().all()


def list_verdicts(db: Session, project_id: int, *, pending_only: bool = False, limit: int = 100) -> dict[str, Any]:
    out = []
    for e in _rows(db, project_id):
        if pending_only and not (e.needs_review and e.is_active):
            continue
        try:
            v = json.loads(e.value_json or "{}")
        except ValueError:
            v = {}
        out.append({"id": e.id, "requirement_id": v.get("requirement_id"), "verdict": v.get("verdict"), "confidence": v.get("confidence"),
                    "blockers": v.get("blockers") or [], "questions_for_customer": v.get("questions_for_customer") or [],
                    "code_evidence": v.get("code_evidence") or [], "data_evidence": v.get("data_evidence") or [], "anchor_corrections": v.get("anchor_corrections") or [],
                    "request_id": v.get("request_id"), "is_active": e.is_active, "needs_review": e.needs_review, "human_review": v.get("human_review")})
    return {"total": len(out), "waiting": sum(1 for x in out if x["needs_review"] and x["is_active"]), "items": out[: max(1, min(limit, 500))]}


def apply(db: Session, project_id: int, entity_id: int, action: str, *, user, reason: str | None = None) -> dict[str, Any]:
    if action not in ("confirm", "reject"):
        raise VerdictError("action: confirm | reject")
    ent = db.get(Entity, entity_id)
    art = db.get(Artifact, ent.artifact_id) if ent else None
    if not ent or not art or art.project_id != project_id or ent.entity_type != "analysis_verdict":
        raise VerdictError("Вердикт не найден")
    try:
        value = json.loads(ent.value_json or "{}")
    except ValueError:
        value = {}
    value["human_review"] = {"by": user.name, "user_id": user.id, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": action,
                             **({"reason": reason.strip()[:500]} if reason else {})}
    ent.needs_review = False
    if action == "reject":
        ent.is_active = False
    ent.value_json = json.dumps(value, ensure_ascii=False)
    db.commit()
    ctr._cache.pop(project_id, None)
    log_activity(db, user_id=user.id, project_id=project_id, block=BLOCK_KNOWLEDGE, action=ACTION_CONFIRM if action == "confirm" else ACTION_REVIEW,
                 title=f"Вердикт агента #{entity_id}: {action}", content=(reason or "")[:200], target_kind="analysis_verdict", target_id=entity_id)
    return {"id": entity_id, "action": action}
