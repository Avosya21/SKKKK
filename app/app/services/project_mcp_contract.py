"""MCP-инструменты контракта обмена с агентом анализа (только чтение). Формат и смысл полей — docs/agent-exchange/CONTRACT.md."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.contract import ContractItem
from app.app.models.project import Project
from app.app.services import contract as ctr


def _built(db: Session, settings: Settings, project_id: int):
    return ctr.get_contract(db, settings, project_id)


def get_requirements(
    db: Session, settings: Settings, project_id: int, *, status: str | None = None, updated_since: str | None = None,
    requirement_ids: list[str] | None = None, limit: int = 100, cursor: str | None = None, fields: str | None = None,
) -> dict[str, Any]:
    payload = dict(_built(db, settings, project_id).files["requirements"])
    items = payload["items"]
    if status:
        items = [r for r in items if r["status"] == status]
    if requirement_ids:
        wanted = set(requirement_ids)
        items = [r for r in items if r["id"] in wanted]
    if updated_since:
        changed = {c["id"] for c in get_changes(db, settings, project_id, since=updated_since, limit=100_000)["items"] if c["change"] != "removed"}
        items = [r for r in items if r["id"] in changed]
    if fields == "brief":  # для маршрутизации: без цитат и длинных текстов (~500 байт вместо ~2 КБ); цитата — через get_quote
        items = [_brief(r) for r in items]
    return ctr.paginate({**payload, "total": len(items), "items": items}, cursor=cursor, limit=limit)


def _brief(r: dict[str, Any]) -> dict[str, Any]:
    ev = r.get("evidence") or []
    return {"id": r["id"], "title": r.get("title"), "status": r["status"], "kind": r.get("kind"), "in_current_tz": r.get("in_current_tz"),
            "verification": r.get("verification"), "code_anchors": [a.get("object") for a in r.get("code_anchors") or []],
            "evidence_sides": [e.get("side") for e in ev], "evidence_count": len(ev), "has_analysis": "analysis" in r,
            "verified_by": (r.get("analysis") or {}).get("verified_by"), "analysis_verdict": (r.get("analysis") or {}).get("verdict")}


def get_conflicts(db: Session, settings: Settings, project_id: int, *, min_severity: str | None = None, status: str | None = None) -> dict[str, Any]:
    payload = dict(_built(db, settings, project_id).files["conflicts"])
    order = {"low": 0, "medium": 1, "high": 2}
    floor = order.get(min_severity or "low", 0)
    items = [c for c in payload["items"] if (not status or c["status"] == status) and order.get(c["severity"], 0) >= floor]
    return {**payload, "total": len(items), "items": items}


def get_tz(db: Session, settings: Settings, project_id: int, *, version: str | None = None) -> dict[str, Any]:
    tz = _built(db, settings, project_id).files["tz"]
    if version and version != tz.get("tz_version"):
        raise ValueError(f"TZ version {version} is not available; current is {tz.get('tz_version')}")
    return tz


def get_artifact_meta(db: Session, settings: Settings, project_id: int, *, artifact_id: str) -> dict[str, Any]:
    for row in _built(db, settings, project_id).files["artifacts"]["items"]:
        if row["artifact_id"] == artifact_id:
            return row
    raise ValueError("Artifact not found")


def get_quote(db: Session, settings: Settings, project_id: int, *, requirement_id: str | None = None, entity_id: int | None = None) -> dict[str, Any]:
    files = _built(db, settings, project_id).files
    for kind in ("requirements", "decisions"):
        for row in files[kind]["items"]:
            if (requirement_id and row["id"] == requirement_id) or (entity_id is not None and row.get("source_entity_id") == entity_id):
                return {"id": row["id"], "statement": row["statement"], "verification": row["verification"], "evidence": row["evidence"],
                        "evidence_missing_reason": row.get("evidence_missing_reason")}
    raise ValueError("Requirement not found")


def get_code_anchors(db: Session, settings: Settings, project_id: int, *, requirement_ids: list[str] | None = None, system: str | None = None) -> dict[str, Any]:
    wanted = set(requirement_ids or [])
    items = []
    for row in _built(db, settings, project_id).files["requirements"]["items"]:
        if wanted and row["id"] not in wanted:
            continue
        anchors = [a for a in row["code_anchors"] if not system or a["system"] == system]
        items.append({"requirement_id": row["id"], "code_anchors": anchors, "code_anchors_reason": row.get("code_anchors_reason")})
    return {"total": len(items), "items": items}


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_changes(db: Session, settings: Settings, project_id: int, *, since: str | None = None, limit: int = 200) -> dict[str, Any]:
    """Дельта контракта после момента since (ISO-8601): added / changed / removed. Без since — всё как added."""
    _built(db, settings, project_id)  # обновляет реестр стабильных id
    cutoff = None
    if since:
        cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
        cutoff = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)
    project = db.get(Project, project_id)
    slug = ctr.project_slug(project.name if project else "", project_id)
    rows = db.scalars(select(ContractItem).where(ContractItem.project_id == project_id).order_by(ContractItem.kind, ContractItem.seq)).all()
    items = []
    for r in rows:
        first, upd, rem = _aware(r.first_seen_at), _aware(r.updated_at), _aware(r.removed_at)
        if rem and (cutoff is None or rem > cutoff):
            change = "removed"
        elif cutoff is None or first > cutoff:
            change = "added"
        elif upd > cutoff:
            change = "changed"
        else:
            continue
        items.append({"id": f"{r.kind}-{slug}-{r.seq:04d}", "change": change, "at": (rem or upd).isoformat()})
    return {"since": since, "total": len(items), "items": items[:limit], "cursor": str(limit) if len(items) > limit else None}
