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


class ReviewNeedsForce(ReviewError):
    """Подтверждение пункта, который модель считает устаревшим или дублем: нужно явное согласие (REQUIREMENTS-PLAN А6)."""


HIDDEN_STATES = ("superseded", "duplicate")  # прячутся из «Ждут вас» на свои вкладки уже в тени (REQUIREMENTS-PLAN А3)
SOURCE_KEYS = ("artifact_id", "material", "material_type", "speaker", "side", "speaker_basis", "said_at", "said_date", "date_basis",
               "timecode", "agreed_by", "quote", "url", "start_sec", "message_id", "paragraph")
CONTEXT_TURNS = 2  # реплик до и после реплики-источника в карточке (всего до 5)


def _model_rejected_rows(db: Session, project_id: int, *, only: str | None = None) -> list[dict[str, Any]]:
    """Требования, выключенные моделью (live) и не возвращённые человеком: их нет в контракте, но их можно «Вернуть».

    ``only="duplicate"`` — только склеенные дубли (вкладка «Склеено»); по умолчанию — отклонённые, кроме заменённых
    (они в контракте со статусом superseded, вкладка «Устарело») и дублей."""
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
        kind = review.get("reject_kind")
        if (only and kind != only) or (not only and kind in HIDDEN_STATES):
            continue
        full = value.get("model_review") or {}
        out.append({"id": f"entity-{ent.id}", "source_entity_id": ent.id, "statement": str(value.get("text") or value.get("description") or ""),
                    "status": "rejected", "status_basis": "model_rejected", "verification": "model_rejected", "kind": ent.entity_type,
                    "estimate": None, "human_review": value.get("human_review"), "analysis": None, "model_review": review, "needs_human": False,
                    "quote": None, "speaker": value.get("speaker"), "side": None, "said_at": None, "artifact_id": art.artifact_id, "evidence_count": 0,
                    "merged_into": full.get("merged_into"), "topic_id": ent.topic_id})
    return out


def hidden_state(r: dict[str, Any]) -> str | None:
    """Заменено / дубль: пункт не ждёт человека (А3). Live-замена — статус superseded; тень — вердикт актуальности."""
    if r.get("status") == "superseded":
        return "superseded"
    rel = r.get("relevance") or {}
    if rel.get("state") in HIDDEN_STATES and not rel.get("undone"):
        return str(rel["state"])
    return None


def is_unsourced(r: dict[str, Any]) -> bool:
    """Нет ни одной цитаты источника (DECISION-MEMORY-PLAN А1): пункт лежит на вкладке «Не подтверждено источником»,
    а не в «Ждут вас». Подтверждённое человеком не трогаем, заменённые и дубли — на своих вкладках."""
    if hidden_state(r) is not None or r.get("verification") == "human_confirmed":
        return False
    return not any((e.get("quote") or "").strip() for e in (r.get("evidence") or []))


def waits_for_human(r: dict[str, Any]) -> bool:
    return hidden_state(r) is None and not is_unsourced(r) and needs_human(r["verification"], r.get("model_review"))


def _context_turns(db: Session, settings: Settings, entity_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Ход обсуждения вокруг реплики-источника: до 5 реплик (REQUIREMENTS-PLAN этап 8). Только пункты, привязанные к реплике."""
    from app.app.services.transcript_turns import format_timecode, load_turns

    out: dict[int, list[dict[str, Any]]] = {}
    cache: dict[int, list] = {}
    for ent in db.scalars(select(Entity).where(Entity.id.in_(entity_ids))).all():
        try:
            value = json.loads(ent.value_json or "{}")
        except ValueError:
            continue
        start = value.get("start_sec") if isinstance(value, dict) else None
        if start is None:
            continue
        if ent.artifact_id not in cache:
            art = db.get(Artifact, ent.artifact_id)
            cache[ent.artifact_id] = [t for t in load_turns(settings, art) if t.is_speech and t.start_sec is not None] if art else []
        turns = cache[ent.artifact_id]
        if not turns:
            continue
        pos = min(range(len(turns)), key=lambda i: abs(turns[i].start_sec - float(start)))
        window = turns[max(0, pos - CONTEXT_TURNS): pos + CONTEXT_TURNS + 1]
        out[ent.id] = [{"speaker": t.known_speaker, "timecode": format_timecode(t.start_sec), "text": t.text[:300], "source": t.index == turns[pos].index}
                       for t in window]
    return out


def _context_units(db: Session, settings: Settings, entity_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Окно вокруг абзаца-источника документа: 3 единицы до, источник и 1 после (Э1). Только пункты, привязанные к абзацу."""
    from app.app.services.doc_units import context_window, split_units
    from app.app.services.storage import artifact_knowledge_text

    out: dict[int, list[dict[str, Any]]] = {}
    cache: dict[int, list] = {}
    for ent in db.scalars(select(Entity).where(Entity.id.in_(entity_ids))).all():
        try:
            value = json.loads(ent.value_json or "{}")
        except ValueError:
            continue
        unit_id = value.get("source_paragraph") if isinstance(value, dict) else None
        if not isinstance(unit_id, int):
            continue
        if ent.artifact_id not in cache:
            art = db.get(Artifact, ent.artifact_id)
            cache[ent.artifact_id] = split_units(artifact_knowledge_text(settings, art) or "") if art else []
        window = context_window(cache[ent.artifact_id], unit_id)
        if window:
            out[ent.id] = [{"speaker": None, "timecode": f"абзац {u.id}", "text": u.text[:300], "source": u.id == unit_id} for u in window]
    return out


def _row(r: dict[str, Any], topics: dict[int, str], by_entity: dict[int, dict[str, Any]]) -> dict[str, Any]:
    ev = r.get("evidence") or []
    first = ev[0] if ev else {}
    rel = r.get("relevance") or {}
    replaced = None
    by_id = r.get("superseded_by_entity") or (rel.get("by") if rel.get("state") in HIDDEN_STATES and not rel.get("undone") else None)
    if by_id and by_id in by_entity:
        other = by_entity[by_id]
        src = (other.get("evidence") or [{}])[0]
        replaced = {"id": other["id"], "statement": other["statement"], **{k: src.get(k) for k in SOURCE_KEYS}}
    return {"id": r["id"], "source_entity_id": r.get("source_entity_id"), "statement": r["statement"], "status": r["status"],
            "status_basis": r.get("status_basis"), "verification": r["verification"], "kind": r.get("kind"), "estimate": r.get("estimate"),
            "human_review": r.get("human_review"), "analysis": r.get("analysis"), "model_review": r.get("model_review"),
            "needs_human": waits_for_human(r), "hidden_state": hidden_state(r), "unsourced": is_unsourced(r),
            "quote": first.get("quote"), "speaker": first.get("speaker"), "side": first.get("side"),
            "said_at": first.get("said_at"), "artifact_id": first.get("artifact_id"), "evidence_count": len(ev),
            # кто и когда по каждому источнику (А9), вид пункта (А7), тема (А2), что заменило (А6)
            "sources": [{k: e.get(k) for k in SOURCE_KEYS} for e in ev],
            "item_kind": r.get("item_kind"), "object": r.get("object"), "unclear_wording": bool(r.get("unclear_wording")),
            "topic": topics.get(r.get("topic_id")) if r.get("topic_id") else None, "topic_id": r.get("topic_id"),
            "relevance": rel or None, "replaced_by": replaced, "merged_from": r.get("merged_from") or []}


def review_list(db: Session, settings: Settings, project_id: int, *, status: str | None = None, basis: str | None = None,
                verification: str | None = None, side: str | None = None, q: str | None = None, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """Очередь требований. verification: needs_human (ждут вас) | unsourced (нет цитаты источника) | superseded (устарело) | merged (склеено) | human_confirmed |
    model_confirmed | extracted | model_rejected. Сортировка — по теме, внутри темы — по порядку в контракте."""
    from app.app.services.topics import topic_labels

    items = ctr.get_contract(db, settings, project_id).files["requirements"]["items"]
    rejected = _model_rejected_rows(db, project_id)
    merged_live = _model_rejected_rows(db, project_id, only="duplicate")
    counts = {
        "total": len(items),
        "unconfirmed": sum(1 for r in items if r["verification"] != "human_confirmed"),
        "needs_human": sum(1 for r in items if waits_for_human(r)),
        "model_confirmed": sum(1 for r in items if r["verification"] == "model_confirmed"),
        "model_rejected": len(rejected),
        "unsourced": sum(1 for r in items if is_unsourced(r)),
        "superseded": sum(1 for r in items if hidden_state(r) == "superseded"),
        "merged": sum(1 for r in items if hidden_state(r) == "duplicate") + len(merged_live),
    }
    topics = topic_labels(db, {r.get("topic_id") for r in items if r.get("topic_id")} | {r.get("topic_id") for r in rejected + merged_live if r.get("topic_id")})
    page = max(1, min(limit, 200))
    if verification == "model_rejected":
        rows = [{**r, "topic": topics.get(r.get("topic_id"))} for r in rejected if not q or q.lower() in r["statement"].lower()]
        return {"total": len(rows), "counts": counts, "items": rows[offset: offset + page]}
    by_entity = {r.get("source_entity_id"): r for r in items}
    rows = []
    for r in items:
        ev = r.get("evidence") or []
        if status and r["status"] != status:
            continue
        if basis and r.get("status_basis") != basis:
            continue
        if verification == "needs_human":
            if not waits_for_human(r):
                continue
        elif verification == "unsourced":
            if not is_unsourced(r):
                continue
        elif verification == "superseded":
            if hidden_state(r) != "superseded":
                continue
        elif verification == "merged":
            if hidden_state(r) != "duplicate":
                continue
        elif verification and r["verification"] != verification:
            continue
        if side and not any(e.get("side") == side for e in ev):
            continue
        if q and q.lower() not in r["statement"].lower():
            continue
        rows.append(_row(r, topics, by_entity))
    if verification == "merged":
        rows += [{**r, "topic": topics.get(r.get("topic_id")), "hidden_state": "duplicate"} for r in merged_live
                 if not q or q.lower() in r["statement"].lower()]
    rows.sort(key=lambda row: (row.get("topic") is None, (row.get("topic") or "").casefold()))
    total = len(rows)
    chunk = rows[offset: offset + page]
    ids = [row["source_entity_id"] for row in chunk if row.get("source_entity_id")]
    context = _context_turns(db, settings, ids)
    for eid, window in _context_units(db, settings, [i for i in ids if i not in context]).items():
        context[eid] = window  # документ: единицы вокруг абзаца
    for row in chunk:
        row["context"] = context.get(row.get("source_entity_id")) or []
    return {"total": total, "counts": counts, "items": chunk}


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
    _undo_relevance(ent, user)
    db.commit()
    ctr._cache.pop(project_id, None)
    log_activity(db, user_id=user.id, project_id=project_id, block=BLOCK_KNOWLEDGE, action=ACTION_REVIEW,
                 title=f"Отменено решение модели по сущности #{entity_id}", content=str(review.get("verdict") or ""),
                 target_kind="entity", target_id=entity_id)
    return {"entity_id": entity_id, "is_active": ent.is_active, "undone": True}


def _undo_relevance(ent: Entity, user) -> None:
    """«Вернуть» снимает и вердикт актуальности (заменено / дубль): модель больше не перебивает решение человека."""
    value = json.loads(ent.value_json or "{}")
    rel = value.get("relevance") if isinstance(value, dict) else None
    if isinstance(rel, dict) and not rel.get("undone"):
        rel["undone"] = {"by": user.name, "user_id": user.id, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        value["relevance"] = rel
        ent.value_json = json.dumps(value, ensure_ascii=False)


def mark_current(db: Session, project_id: int, entity_id: int, *, user) -> dict[str, Any]:
    """«Это актуально»: снять вердикт модели «заменено / дубль» (в тени — только вердикт; в live — ещё и «Вернуть»)."""
    ent = db.get(Entity, entity_id)
    art = db.get(Artifact, ent.artifact_id) if ent else None
    if not ent or not art or art.project_id != project_id or ent.entity_type not in ("decision", "task"):
        raise ReviewError("Требование не найдено")
    review = public_model_review(json.loads(ent.value_json or "{}")) or {}
    if review.get("deactivated") and not review.get("undone"):
        return undo_model(db, settings=None, project_id=project_id, entity_id=entity_id, user=user)  # type: ignore[arg-type]
    from app.app.services.review_relevance import mark_current as relevance_current

    try:
        result = relevance_current(db, entity_id, user_name=user.name, user_id=user.id)
    except ValueError as exc:
        raise ReviewError(str(exc)) from exc
    db.commit()
    ctr._cache.pop(project_id, None)
    log_activity(db, user_id=user.id, project_id=project_id, block=BLOCK_KNOWLEDGE, action=ACTION_REVIEW,
                 title=f"Пункт #{entity_id} актуален (снят вердикт модели «{result.get('state')}»)", content="",
                 target_kind="entity", target_id=entity_id)
    return result


def _entity_for(db: Session, settings: Settings, project_id: int, requirement_id: str) -> Entity:
    for r in ctr.get_contract(db, settings, project_id).files["requirements"]["items"]:
        if r["id"] == requirement_id:
            ent = db.get(Entity, r["source_entity_id"])
            art = db.get(Artifact, ent.artifact_id) if ent else None
            if ent and art and art.project_id == project_id and ent.entity_type in ("decision", "task"):
                return ent
    raise ReviewError("Требование не найдено")


def apply(db: Session, settings: Settings, project_id: int, requirement_id: str, action: str, *, user, reason: str | None = None,
          status: str | None = None, force: bool = False) -> dict[str, Any]:
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
        rel = value.get("relevance") if isinstance(value.get("relevance"), dict) else {}
        if not force and rel.get("state") in HIDDEN_STATES and not rel.get("undone"):
            what = "устаревшим (позже заменён)" if rel["state"] == "superseded" else "дублем другого пункта"
            raise ReviewNeedsForce(f"Модель считает пункт {what}. Подтверждение закрепит его: если отказ был раньше вашего "
                                   f"подтверждения, система уже не снимет пункт сама. Подтвердить всё равно?")
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
        except ReviewNeedsForce:
            failed.append({"id": rid, "why": "модель считает пункт устаревшим или дублем — подтвердите его отдельно"})
        except ReviewError as exc:
            failed.append({"id": rid, "why": str(exc)})
    return {"confirmed": done, "failed": failed}
