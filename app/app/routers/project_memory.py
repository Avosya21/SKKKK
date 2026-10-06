"""Память решений проекта: предметы, их лента и состояние на дату, решение человека (docs/plans/DECISION-MEMORY-PLAN.md, Э6).

Смотрят и решают админ и менеджер проекта. Решение человека — событие журнала (``decided_by=human``): после него модель и правила
состояние предмета не меняют, пока человек не вернёт предмет кнопкой «Вернуть модели».
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.app.db import get_db
from app.app.dependencies import get_current_user
from app.app.models.artifact import Artifact
from app.app.models.decision_memory import EVENT_KINDS, EVENT_LABELS, POSITION_LABELS, STATE_LABELS, DecisionEvent, Subject, SubjectPosition
from app.app.models.speech_act import ACT_LABELS, SpeechAct
from app.app.models.user import User
from app.app.services import decision_log as dl
from app.app.services.project_access import require_project_staff
from app.app.services.source_links import source_url

router = APIRouter(prefix="/projects", tags=["project-memory"])
HUMAN_KINDS = ("agreed", "rejected", "implemented", "withdrawn", "superseded", "reopened")
PAGE_MAX = 200


class DecideIn(BaseModel):
    kind: str
    note: str | None = Field(default=None, max_length=300)


def _subject(db: Session, project_id: int, subject_id: int) -> Subject:
    row = db.get(Subject, subject_id)
    if row is None or row.project_id != project_id or not row.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Предмет не найден")
    return row


def _summary(s: Subject, acts: int, last_at: str | None) -> dict[str, Any]:
    return {"id": s.id, "title": s.title, "state": s.state, "state_label": STATE_LABELS.get(s.state, s.state), "since": s.state_since,
            "source": s.state_source, "summary": s.summary, "acts": acts, "last_at": last_at}


@router.get("/{project_id}/memory/subjects")
def list_subjects(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    state: Annotated[str | None, Query(max_length=16)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    require_project_staff(db, user, project_id)
    where = [Subject.project_id == project_id, Subject.is_active.is_(True)]
    if state:
        where.append(Subject.state == state)
    if q and q.strip():
        like = f"%{q.strip()}%"
        where.append(or_(Subject.title.ilike(like), Subject.summary.ilike(like)))
    total = db.scalar(select(func.count()).select_from(Subject).where(*where)) or 0
    counts = dict(db.execute(select(Subject.state, func.count()).where(Subject.project_id == project_id, Subject.is_active.is_(True)).group_by(Subject.state)).all())
    rows = db.scalars(select(Subject).where(*where).order_by(Subject.state_since.desc().nulls_last(), Subject.id.desc()).limit(limit).offset(offset)).all()
    stats = {sid: (n, last) for sid, n, last in db.execute(select(SpeechAct.subject_id, func.count(), func.max(SpeechAct.said_at))
                                                          .where(SpeechAct.subject_id.in_([r.id for r in rows] or [-1])).group_by(SpeechAct.subject_id)).all()}
    return {"total": total, "counts": counts, "labels": STATE_LABELS, "items": [_summary(r, *(stats.get(r.id) or (0, None))) for r in rows]}


def _act_out(db: Session, a: SpeechAct, names: dict[int, tuple[str, str]]) -> dict[str, Any]:
    aid, fname = names.get(a.artifact_id, ("", ""))
    url = None
    if aid:
        url = source_url(aid, start_sec=a.start_sec, message_id=int(a.unit_key) if a.unit_ref.startswith("M") else None, quote=a.quote)
    return {"id": a.id, "ref": a.unit_ref, "at": a.said_at, "author": a.author, "side": a.side, "act": a.act, "act_label": ACT_LABELS.get(a.act, a.act),
            "quote": a.quote, "about": a.about, "material": fname, "url": url}


@router.get("/{project_id}/memory/subjects/{subject_id}")
def get_subject(
    project_id: int, subject_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    as_of: Annotated[str | None, Query(max_length=10)] = None,
):
    """Предмет: лента актов с цитатами и ссылками на источник, события журнала, как менялось состояние, состояние на дату."""
    require_project_staff(db, user, project_id)
    s = _subject(db, project_id, subject_id)
    acts = db.scalars(select(SpeechAct).where(SpeechAct.subject_id == s.id).order_by(SpeechAct.said_at, SpeechAct.artifact_id, SpeechAct.unit_key)).all()
    names = {a.id: (a.artifact_id, a.original_filename) for a in db.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()}
    events = dl.events_of(db, s.id)
    fold = dl.fold(events, as_of=as_of) if as_of else dl.fold(events)
    return {
        "subject": _summary(s, len(acts), acts[-1].said_at if acts else None),
        "acts": [_act_out(db, a, names) for a in acts],
        "events": [{"id": e.id, "kind": e.kind, "kind_label": EVENT_LABELS.get(e.kind, e.kind), "at": e.occurred_at, "precision": e.precision, "actor": e.actor_label,
                    "side": e.actor_side, "anchor": e.anchor, "quote": e.quote, "decided_by": e.decided_by, "note": e.note} for e in events],
        "positions": [{"label": p.label, "state": p.state, "state_label": POSITION_LABELS.get(p.state, p.state), "replaced_by": p.replaced_by}
                      for p in db.scalars(select(SubjectPosition).where(SubjectPosition.subject_id == s.id).order_by(SubjectPosition.id)).all()],
        "fold": {**fold.as_dict(), "state_label": STATE_LABELS.get(fold.state, fold.state)},
        "as_of": as_of,
    }


@router.get("/{project_id}/memory/state-at")
def state_at_date(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    date: Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")],
):
    """Состояние всех предметов проекта на дату (до конца этого дня): что было решено к этому дню."""
    require_project_staff(db, user, project_id)
    out = []
    for s in db.scalars(select(Subject).where(Subject.project_id == project_id, Subject.is_active.is_(True)).order_by(Subject.id)).all():
        res = dl.state_at(db, s.id, date)
        if res.state != "open" or res.trail:
            out.append({"id": s.id, "title": s.title, "state": res.state, "state_label": STATE_LABELS.get(res.state, res.state), "since": res.since,
                        "flags": res.flags, "source": res.source})
    return {"date": date, "items": out}


@router.post("/{project_id}/memory/subjects/{subject_id}/decide")
def decide(
    project_id: int, subject_id: int, body: DecideIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Решение человека по предмету: пишется событием журнала; модель и правила состояние после него не меняют."""
    require_project_staff(db, user, project_id)
    if body.kind not in HUMAN_KINDS or body.kind not in EVENT_KINDS:
        raise HTTPException(status_code=400, detail=f"Решение: одно из {', '.join(HUMAN_KINDS)}")
    s = _subject(db, project_id, subject_id)
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    note = (body.note or "").strip() or None
    dl.add_event(db, s, body.kind, now, actor_label=user.name, decided_by="human", note=note)
    res = dl.refresh_subject(db, s)
    db.commit()
    return {"subject": _summary(s, 0, None), "fold": res.as_dict()}


@router.post("/{project_id}/memory/subjects/{subject_id}/release")
def release(
    project_id: int, subject_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """«Вернуть модели»: события человека по предмету отключаются, состояние снова считается по журналу."""
    require_project_staff(db, user, project_id)
    s = _subject(db, project_id, subject_id)
    n = 0
    for e in db.scalars(select(DecisionEvent).where(DecisionEvent.subject_id == s.id, DecisionEvent.decided_by == "human", DecisionEvent.is_active.is_(True))).all():
        e.is_active = False
        n += 1
    res = dl.refresh_subject(db, s)
    db.commit()
    return {"released": n, "subject": _summary(s, 0, None), "fold": res.as_dict()}
