"""Задачи проекта под управлением менеджера (docs/plans/PORTAL-ACCURACY-MANAGER-PLAN.md, Ш4).

Извлечённая задача — сущность ``entity_type=task``; правка человека пишется в ``value_json.human_edit`` поверх извлечённого
(извлечённое остаётся, видно «было → стало»). Это не подтверждение требования: ``human_review`` и очередь «Ждут вас» правка не
трогает. При переизвлечении правка переносится на новый пункт с той же мыслью (``entities._transfer_human``). Ручные задачи —
таблица ``manual_tasks``. Устаревшие и склеенные пункты (вердикт актуальности) в списке не показываются.

Задачи из ТЗ, описаний и договоров (положения: «сформировать отчёт…») и из планов (этапы графика «до февраля») — не
поручения людям: они на вкладке «ТЗ и план» и не входят в открытые, просроченные, недельные и «Что нового». Вид документа —
из его паспорта (``<artifact_id>_passport.json``, ``doc_memory``). Протоколы, отчёты о статусе, переписка, созвоны — обычные задачи.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.models.manual_task import ManualTask

STATUSES = ("open", "in_progress", "done", "cancelled")
STATUS_LABELS = {"open": "открыта", "in_progress": "в работе", "done": "сделано", "cancelled": "отменена"}
CLOSED = ("done", "cancelled")
VIEWS = ("open", "overdue", "week", "no_deadline", "reference", "done", "all")
SPEC_DOC_KINDS = ("tz", "spec", "contract")  # положения документа
PLAN_DOC_KINDS = ("plan",)  # этапы плана
REFERENCE_ORIGINS = ("spec", "plan")
ORIGIN_LABELS = {"spec": "из ТЗ", "plan": "из плана"}
EDIT_FIELDS = ("status", "assignee", "deadline_at")
HIDDEN_RELEVANCE = ("superseded", "duplicate")
MAX_TEXT = 500


class TaskError(ValueError):
    pass


def normalize_status(raw: Any) -> str:
    """Статус извлечения (open / unknown / done / closed / cancelled / …) → один из STATUSES; неизвестный — «открыта»."""
    key = str(raw or "").strip().lower()
    if key in ("done", "closed", "completed", "implemented"):
        return "done"
    if key in ("cancelled", "canceled", "rejected"):
        return "cancelled"
    if key in ("in_progress", "doing", "started"):
        return "in_progress"
    return "open"


def hidden(value: dict[str, Any]) -> bool:
    """Пункт заменён или склеен (вердикт актуальности, не отменённый человеком) — в списке задач не показывается."""
    rel = value.get("relevance")
    return isinstance(rel, dict) and rel.get("state") in HIDDEN_RELEVANCE and not rel.get("undone")


def effective(value: dict[str, Any]) -> dict[str, Any]:
    """Значения задачи с правкой человека поверх извлечённого: assignee, deadline, deadline_at, status."""
    out = dict(value)
    edit = value.get("human_edit") if isinstance(value.get("human_edit"), dict) else {}
    for key in ("assignee", "status"):
        if key in edit:
            out[key] = edit[key]
    if "deadline_at" in edit:
        out["deadline_at"] = edit["deadline_at"]
        out["deadline"] = _deadline_text(edit["deadline_at"])
    return out


def _deadline_text(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return None


def _clean_date(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise TaskError("Срок — дата ГГГГ-ММ-ДД") from exc


def _clean_text(raw: Any, limit: int = 200) -> str | None:
    text = " ".join(str(raw or "").split())
    return text[:limit] or None


def _today() -> date:
    from app.app.services.recorded_at import LOCAL_TZ

    return datetime.now(LOCAL_TZ).date()


# ---------- список ----------

def _row_dates(row: dict[str, Any], ref: date | None) -> None:
    from app.app.services.project_deadlines import normalize_deadline_at, resolve_due

    start, end, due_time = resolve_due(row.get("deadline"), normalize_deadline_at(row.get("deadline_at")), ref)
    row["deadline_date"] = start.isoformat() if start else None
    row["deadline_end_date"] = end.isoformat() if end else None
    row["deadline_time"] = due_time


def _doc_kind(art: Artifact, cache: dict[int, str | None]) -> str | None:
    """Вид документа из паспорта (tz / plan / status_report / …); нет паспорта — None."""
    if art.id in cache:
        return cache[art.id]
    kind = None
    if art.source_type != "call":
        from app.app.config import get_settings
        from app.app.services.storage import parsed_dir

        path = parsed_dir(get_settings(), art.project_id, art.source_type) / f"{art.artifact_id}_passport.json"
        try:
            kind = (json.loads(path.read_text(encoding="utf-8")) or {}).get("kind") if path.is_file() else None
        except (OSError, ValueError):
            kind = None
    cache[art.id] = kind
    return kind


def task_origin(art: Artifact, cache: dict[int, str | None]) -> str:
    """Откуда задача: call / document / spec (положение ТЗ) / plan (этап плана)."""
    if art.source_type == "call":
        return "call"
    kind = _doc_kind(art, cache)
    if kind in SPEC_DOC_KINDS:
        return "spec"
    if kind in PLAN_DOC_KINDS:
        return "plan"
    return "document"


def _extracted_rows(db: Session, project_id: int) -> list[dict[str, Any]]:
    from app.app.services.project_deadlines import (
        _deadline_is_empty,
        _human_assignee,
        artifact_reference_date,
        reference_is_explicit,
    )

    rows = db.execute(
        select(Entity, Artifact).join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.is_active.is_(True), Entity.entity_type == "task")
        .order_by(Entity.id)
    ).all()
    out: list[dict[str, Any]] = []
    kinds: dict[int, str | None] = {}
    for ent, art in rows:
        try:
            raw = json.loads(ent.value_json or "{}")
        except ValueError:
            continue
        if not isinstance(raw, dict) or hidden(raw):
            continue
        value = effective(raw)
        description = str(value.get("description") or value.get("text") or "").strip()
        if not description:
            continue
        deadline = None if _deadline_is_empty(str(value.get("deadline")) if value.get("deadline") is not None else None) \
            else str(value.get("deadline")).strip()
        edit = raw.get("human_edit") if isinstance(raw.get("human_edit"), dict) else None
        ref = artifact_reference_date(art)
        row = {
            "id": f"e{ent.id}", "entity_id": ent.id, "source_kind": "extracted", "description": description,
            "assignee": _human_assignee(str(value.get("assignee")).strip() if value.get("assignee") else None),
            "deadline": deadline, "deadline_at": value.get("deadline_at"),
            "status": normalize_status(value.get("status")),
            "date_approximate": bool(deadline) and not (edit and "deadline_at" in edit) and not value.get("deadline_at")
            and not reference_is_explicit(art),
            "artifact_id": art.artifact_id, "filename": art.original_filename,
            "transcript_html_url": art.transcript_html_url if art.status == "done" else None,
            "task_kind": value.get("kind"),
            "origin": task_origin(art, kinds),
            "edited": {"by": edit.get("by"), "at": edit.get("at"), "fields": edit.get("fields") or [],
                       "before": edit.get("before") or {}} if edit else None,
        }
        _row_dates(row, ref)
        out.append(row)
    return out


def _manual_rows(db: Session, project_id: int) -> list[dict[str, Any]]:
    out = []
    for t in db.scalars(select(ManualTask).where(ManualTask.project_id == project_id, ManualTask.is_active.is_(True))
                        .order_by(ManualTask.id)).all():
        row = {"id": f"m{t.id}", "manual_id": t.id, "source_kind": "manual", "description": t.description, "assignee": t.assignee,
               "deadline": _deadline_text(t.deadline_at), "deadline_at": t.deadline_at, "status": normalize_status(t.status),
               "date_approximate": False, "artifact_id": None, "filename": None, "transcript_html_url": None, "task_kind": None,
               "origin": "manual", "edited": None}
        _row_dates(row, None)
        out.append(row)
    return out


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.app.services.project_deadlines import _normalize_key

    seen: set[tuple[str, str, str]] = set()
    out = []
    for row in rows:
        key = (_normalize_key(row["description"]), _normalize_key(row.get("assignee") or ""), _normalize_key(row.get("deadline") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _bucket(row: dict[str, Any], today: date) -> dict[str, bool]:
    closed = row["status"] in CLOSED
    if row.get("origin") in REFERENCE_ORIGINS:  # положение ТЗ или этап плана — не поручение: только своя вкладка
        return {"closed": closed, "overdue": False, "week": False, "no_deadline": False, "reference": not closed}
    due = date.fromisoformat(row["deadline_date"]) if row.get("deadline_date") else None
    return {"closed": closed, "overdue": bool(due and not closed and due < today),
            "week": bool(due and not closed and today <= due <= today + timedelta(days=7)),
            "no_deadline": not closed and not row.get("deadline"), "reference": False}


def all_tasks(db: Session, project_id: int, *, today: date | None = None) -> list[dict[str, Any]]:
    """Все видимые задачи проекта (извлечённые с правками и ручные) с признаками просрочки и недели."""
    today = today or _today()
    rows = _dedupe(_extracted_rows(db, project_id) + _manual_rows(db, project_id))
    for row in rows:
        flags = _bucket(row, today)
        row.update(overdue=flags["overdue"], this_week=flags["week"], reference=flags["reference"] or row.get("origin") in REFERENCE_ORIGINS,
                   origin_label=ORIGIN_LABELS.get(row.get("origin"), ""), status_label=STATUS_LABELS[row["status"]])
    return rows


def list_project_tasks(db: Session, project_id: int, *, view: str = "open", assignee: str | None = None,
                       offset: int = 0, limit: int = 50, today: date | None = None) -> dict[str, Any]:
    if view not in VIEWS:
        raise TaskError(f"view: один из {', '.join(VIEWS)}")
    today = today or _today()
    rows = all_tasks(db, project_id, today=today)
    flags = {row["id"]: _bucket(row, today) for row in rows}
    counts = {
        "open": sum(1 for r in rows if not flags[r["id"]]["closed"] and not flags[r["id"]]["reference"]),
        "overdue": sum(1 for r in rows if flags[r["id"]]["overdue"]),
        "week": sum(1 for r in rows if flags[r["id"]]["week"]),
        "no_deadline": sum(1 for r in rows if flags[r["id"]]["no_deadline"]),
        "reference": sum(1 for r in rows if flags[r["id"]]["reference"]),
        "done": sum(1 for r in rows if flags[r["id"]]["closed"]),
        "all": len(rows),
    }
    pick = {
        "open": lambda r: not flags[r["id"]]["closed"] and not flags[r["id"]]["reference"], "overdue": lambda r: flags[r["id"]]["overdue"],
        "week": lambda r: flags[r["id"]]["week"], "no_deadline": lambda r: flags[r["id"]]["no_deadline"],
        "reference": lambda r: flags[r["id"]]["reference"],
        "done": lambda r: flags[r["id"]]["closed"], "all": lambda r: True,
    }[view]
    chosen = [r for r in rows if pick(r)]
    assignees = sorted({r["assignee"] for r in rows if r.get("assignee")}, key=str.casefold)
    if assignee:
        chosen = [r for r in chosen if (r.get("assignee") or "").casefold() == assignee.casefold()]
    chosen.sort(key=lambda r: (r.get("deadline_date") is None, r.get("deadline_date") or "", r["description"].casefold()))
    page = max(1, min(limit, 200))
    return {"view": view, "today": today.isoformat(), "total": len(chosen), "counts": counts, "assignees": assignees,
            "statuses": [{"key": k, "label": STATUS_LABELS[k]} for k in STATUSES], "items": chosen[offset: offset + page]}


# ---------- правка ----------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "status" in body:
        status = str(body.get("status") or "")
        if status not in STATUSES:
            raise TaskError(f"status: один из {', '.join(STATUSES)}")
        changes["status"] = status
    if "assignee" in body:
        changes["assignee"] = _clean_text(body.get("assignee"))
    if "deadline_at" in body:
        changes["deadline_at"] = _clean_date(body.get("deadline_at"))
    if not changes:
        raise TaskError("Нечего менять: status, assignee или deadline_at")
    return changes


def edit_extracted(db: Session, project_id: int, entity_id: int, body: dict[str, Any], *, user) -> dict[str, Any]:
    """Правка извлечённой задачи: поля поверх извлечённого в ``human_edit``; первое «было» запоминается в ``before``."""
    ent = db.get(Entity, entity_id)
    art = db.get(Artifact, ent.artifact_id) if ent else None
    if not ent or not art or art.project_id != project_id or ent.entity_type != "task" or not ent.is_active:
        raise TaskError("Задача не найдена")
    changes = _changes(body)
    value = json.loads(ent.value_json or "{}")
    edit = dict(value.get("human_edit") or {})
    before = dict(edit.get("before") or {})
    for key in changes:
        if key not in before:
            before[key] = normalize_status(value.get("status")) if key == "status" else value.get(key)
    fields = sorted(set(edit.get("fields") or []) | set(changes))
    edit.update(changes)
    edit.update(before=before, fields=fields, by=user.name, user_id=user.id, at=_now())
    reason = _clean_text(body.get("reason"), MAX_TEXT)
    if reason:
        edit["reason"] = reason
    value["human_edit"] = edit
    ent.value_json = json.dumps(value, ensure_ascii=False)
    db.commit()
    return {"id": f"e{entity_id}", "human_edit": edit}


def create_manual(db: Session, project_id: int, body: dict[str, Any], *, user) -> dict[str, Any]:
    description = _clean_text(body.get("description"), MAX_TEXT)
    if not description:
        raise TaskError("Опишите задачу")
    status = str(body.get("status") or "open")
    if status not in STATUSES:
        raise TaskError(f"status: один из {', '.join(STATUSES)}")
    task = ManualTask(project_id=project_id, description=description, assignee=_clean_text(body.get("assignee")),
                      deadline_at=_clean_date(body.get("deadline_at")), status=status, created_by=user.id)
    db.add(task)
    db.commit()
    return {"id": f"m{task.id}", "manual_id": task.id}


def _manual(db: Session, project_id: int, task_id: int) -> ManualTask:
    task = db.get(ManualTask, task_id)
    if not task or task.project_id != project_id or not task.is_active:
        raise TaskError("Задача не найдена")
    return task


def edit_manual(db: Session, project_id: int, task_id: int, body: dict[str, Any]) -> dict[str, Any]:
    task = _manual(db, project_id, task_id)
    if "description" in body:
        description = _clean_text(body.get("description"), MAX_TEXT)
        if not description:
            raise TaskError("Опишите задачу")
        task.description = description
        body = {k: v for k, v in body.items() if k != "description"}
        if not any(k in body for k in EDIT_FIELDS):
            db.commit()
            return {"id": f"m{task.id}"}
    changes = _changes(body)
    for key, val in changes.items():
        setattr(task, key, val)
    db.commit()
    return {"id": f"m{task.id}"}


def delete_manual(db: Session, project_id: int, task_id: int) -> dict[str, Any]:
    task = _manual(db, project_id, task_id)
    task.is_active = False
    db.commit()
    return {"id": f"m{task.id}", "deleted": True}
