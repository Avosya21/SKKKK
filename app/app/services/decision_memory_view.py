"""Память решений для чтения: список предметов, предмет с лентой и журналом, состояние на дату.

Общая логика экрана памяти (`routers/project_memory.py`) и инструмента MCP `get_decisions` (docs/plans/MCP-KB-PLAN.md, Э5):
одни и те же данные, права проверяет вызывающий. У каждой реплики — материал (`artifact_id`, `ref`), чтобы внешний агент
мог открыть источник через `get_artifact_content`, и ссылка портала (`url`) для человека.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact
from app.app.models.decision_memory import EVENT_LABELS, POSITION_LABELS, STATE_LABELS, DecisionEvent, Subject, SubjectPosition
from app.app.models.speech_act import ACT_LABELS, SpeechAct
from app.app.services import decision_log as dl
from app.app.services.source_links import source_url


def summary_row(s: Subject, acts: int, last_at: str | None, *, human: bool = False, positions: int = 0) -> dict[str, Any]:
    return {"id": s.id, "title": s.title, "state": s.state, "state_label": STATE_LABELS.get(s.state, s.state), "since": s.state_since,
            "source": s.state_source, "summary": s.summary, "acts": acts, "last_at": last_at, "human": human, "positions": positions}


def state_counts(db: Session, project_id: int) -> dict[str, int]:
    return dict(db.execute(select(Subject.state, func.count()).where(Subject.project_id == project_id, Subject.is_active.is_(True))
                           .group_by(Subject.state)).all())


def active_subject(db: Session, project_id: int, subject_id: int) -> Subject | None:
    row = db.get(Subject, subject_id)
    return row if row is not None and row.project_id == project_id and row.is_active else None


def list_subjects(db: Session, project_id: int, *, state: str | None = None, q: str | None = None, ids: list[int] | None = None,
                  limit: int | None = 50, offset: int = 0) -> dict[str, Any]:
    """Предметы проекта (новые сверху): фильтр по состоянию, словам в названии/итоге или списку id; число реплик, решено ли человеком."""
    where = [Subject.project_id == project_id, Subject.is_active.is_(True)]
    if state:
        where.append(Subject.state == state)
    if q and q.strip():
        like = f"%{q.strip()}%"
        where.append(or_(Subject.title.ilike(like), Subject.summary.ilike(like)))
    if ids is not None:
        where.append(Subject.id.in_(ids or [-1]))
    total = db.scalar(select(func.count()).select_from(Subject).where(*where)) or 0
    stmt = select(Subject).where(*where).order_by(Subject.state_since.desc().nulls_last(), Subject.id.desc()).offset(offset)
    rows = db.scalars(stmt.limit(limit) if limit else stmt).all()
    sid = [r.id for r in rows] or [-1]
    stats = {k: (n, last) for k, n, last in db.execute(select(SpeechAct.subject_id, func.count(), func.max(SpeechAct.said_at))
                                                        .where(SpeechAct.subject_id.in_(sid)).group_by(SpeechAct.subject_id)).all()}
    humans = set(db.scalars(select(DecisionEvent.subject_id).where(DecisionEvent.subject_id.in_(sid), DecisionEvent.decided_by == "human",
                                                                  DecisionEvent.is_active.is_(True))).all())
    pos = dict(db.execute(select(SubjectPosition.subject_id, func.count()).where(SubjectPosition.subject_id.in_(sid)).group_by(SubjectPosition.subject_id)).all())
    return {"total": total, "counts": state_counts(db, project_id), "labels": STATE_LABELS,
            "items": [summary_row(r, *(stats.get(r.id) or (0, None)), human=r.id in humans, positions=pos.get(r.id, 0)) for r in rows]}


def _act_out(a: SpeechAct, names: dict[int, tuple[str, str]]) -> dict[str, Any]:
    aid, fname = names.get(a.artifact_id, ("", ""))
    url = None
    if aid:
        url = source_url(aid, start_sec=a.start_sec, message_id=int(a.unit_key) if a.unit_ref.startswith("M") else None, quote=a.quote)
    return {"id": a.id, "ref": a.unit_ref, "at": a.said_at, "author": a.author, "side": a.side, "act": a.act, "act_label": ACT_LABELS.get(a.act, a.act),
            "quote": a.quote, "about": a.about, "material": fname, "artifact_id": aid, "url": url}


def dedupe_acts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Одна и та же реплика из двух выгрузок одной переписки показывается один раз, со всеми ссылками на источник (`urls`)."""
    seen: dict[tuple, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for it in items:
        key = (it["at"], it["author"], it["act"], " ".join((it["quote"] or "").split()))
        if key in seen:
            if it["url"] and it["url"] not in seen[key]["urls"]:
                seen[key]["urls"].append(it["url"])
            continue
        it["urls"] = [it["url"]] if it["url"] else []
        seen[key] = it
        out.append(it)
    return out


def subject_detail(db: Session, project_id: int, s: Subject, *, as_of: str | None = None) -> dict[str, Any]:
    """Предмет: лента реплик с цитатами и источником, события журнала, варианты, итог (на дату as_of, если задана)."""
    acts = db.scalars(select(SpeechAct).where(SpeechAct.subject_id == s.id).order_by(SpeechAct.said_at, SpeechAct.artifact_id, SpeechAct.unit_key)).all()
    names = {a.id: (a.artifact_id, a.original_filename) for a in db.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()}
    events = dl.events_of(db, s.id)
    fold = dl.fold(events, as_of=as_of) if as_of else dl.fold(events)
    return {
        "subject": summary_row(s, len(acts), acts[-1].said_at if acts else None),
        "acts": dedupe_acts([_act_out(a, names) for a in acts]),
        "events": [{"id": e.id, "kind": e.kind, "kind_label": EVENT_LABELS.get(e.kind, e.kind), "at": e.occurred_at, "precision": e.precision, "actor": e.actor_label,
                    "side": e.actor_side, "anchor": e.anchor, "quote": e.quote, "decided_by": e.decided_by, "note": e.note} for e in events],
        "positions": [{"label": p.label, "state": p.state, "state_label": POSITION_LABELS.get(p.state, p.state), "replaced_by": p.replaced_by}
                      for p in db.scalars(select(SubjectPosition).where(SubjectPosition.subject_id == s.id).order_by(SubjectPosition.id)).all()],
        "fold": {**fold.as_dict(), "state_label": STATE_LABELS.get(fold.state, fold.state)},
        "as_of": as_of,
    }


def state_at(db: Session, project_id: int, date: str) -> dict[str, Any]:
    """Состояние всех предметов проекта на дату (до конца этого дня): что было решено к этому дню."""
    out = []
    for s in db.scalars(select(Subject).where(Subject.project_id == project_id, Subject.is_active.is_(True)).order_by(Subject.id)).all():
        res = dl.state_at(db, s.id, date)
        if res.state != "open" or res.trail:
            out.append({"id": s.id, "title": s.title, "state": res.state, "state_label": STATE_LABELS.get(res.state, res.state), "since": res.since,
                        "flags": res.flags, "source": res.source})
    return {"date": date, "items": out}
