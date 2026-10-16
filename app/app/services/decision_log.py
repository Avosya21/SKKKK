"""Журнал событий и состояние предмета на дату (docs/plans/DECISION-MEMORY-PLAN.md, Э4). Чистый код, модель не вызывается.

Состояние — свёртка событий по времени (``fold``). Правила:
- ``proposed`` открывает предложение (в том числе после отказа или снятия: «предложили снова»);
- ``documented`` — пункт записан в документе: «в документе» (из «обсуждается», «предложено», после отказа, снятия или замены);
  согласованное и выполненное документ не понижает. Дальше «в документе» ведёт себя как предложение стороны документа:
  его принимает согласие заказчика или другой стороны, отклоняет отказ;
- ``agreed`` — согласовано, если согласился заказчик либо сторона, отличная от предложившей («заказчик попросил — исполнитель
  согласился» — обязательство принято); согласие своей же стороны (исполнитель согласился с исполнителем) предложение не
  закрывает, и оно остаётся ``proposed`` с пометкой. Во внутреннем проекте (``internal_project`` в настройках: все на стороне
  исполнителя) согласие засчитывается от любого другого человека, кроме автора предложения (``actor_person_id``, без него —
  ``actor_label``);
- ``rejected`` — отклонено, если отказала другая сторона; своей же стороны — это снятие (``withdrawn``);
- ``superseded`` / ``withdrawn`` / ``implemented`` / ``reopened`` меняют состояние как написано; ``reopened`` возвращает в ``proposed``;
- событие человека (``decided_by=human``) главнее: после него события модели и правил состояние не меняют, пока человек не скажет иначе.
Порядок — по ``occurred_at`` (ISO), затем по номеру. Если два разных события одного дня имеют только день, порядок между ними
неизвестен — результат помечается ``ambiguous_order``. Состояние «на дату» — свёртка событий до конца этого дня.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.decision_memory import DecisionEvent, Subject

SIDE_CUSTOMER = "customer"
# Какой вид акта речи становится каким событием; остальные виды (вопрос, ответ, отсрочка, препятствие, факт) — контекст
ACT_TO_EVENT = {"proposed": "proposed", "agreed": "agreed", "rejected": "rejected", "done": "implemented", "cancelled": "withdrawn",
                "documented": "documented"}
OPEN_OFFERS = ("proposed", "documented")  # ждут ответа другой стороны


def event_kind(act: Any) -> str | None:
    """Вид события для акта или None (контекст). Факт из документа («реквизиты», «источник финансирования») — тоже записанное
    в документе условие: «в документе», а не «обсуждается» (проект 40, 2026-10-09). Факт из реплики — по-прежнему контекст."""
    kind = ACT_TO_EVENT.get(act.act)
    if kind is None and act.act == "info" and str(getattr(act, "unit_ref", "") or "").startswith("P"):
        return "documented"
    return kind


@dataclass
class FoldResult:
    state: str = "open"
    since: str | None = None
    basis_event_id: int | None = None
    source: str = "fold"  # fold | human
    flags: list[str] = field(default_factory=list)
    trail: list[dict[str, Any]] = field(default_factory=list)  # как менялось состояние: событие, откуда, куда

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "since": self.since, "basis_event_id": self.basis_event_id, "source": self.source,
                "flags": self.flags, "trail": self.trail}


def _sort_key(e: Any) -> tuple[str, int]:
    return (str(e.occurred_at), int(e.id or 0))


def _on_or_before(occurred_at: str, as_of: str) -> bool:
    """Событие входит в состояние «на дату»: дата-только сравнивается по дню (весь день включён)."""
    return occurred_at[:10] <= as_of[:10] if len(as_of) <= 10 else occurred_at <= as_of


def _actor_key(e: Any) -> tuple[str, Any] | None:
    """Кто сказал: человек реестра, без него — подпись автора; None, если неизвестно."""
    person = getattr(e, "actor_person_id", None)
    if person:
        return ("person", int(person))
    label = " ".join(str(getattr(e, "actor_label", None) or "").split()).casefold()
    return ("label", label) if label else None


def fold(events: Iterable[Any], *, as_of: str | None = None, internal: bool = False) -> FoldResult:
    """Состояние предмета по событиям. ``as_of`` — ГГГГ-ММ-ДД (до конца дня) или ISO-момент. ``internal`` — внутренний проект:
    согласие засчитывается от любого другого человека, кроме автора предложения."""
    items = sorted((e for e in events if getattr(e, "is_active", True)), key=_sort_key)
    if as_of:
        items = [e for e in items if _on_or_before(str(e.occurred_at), as_of)]
    res = FoldResult()
    proposer_side: str | None = None
    proposer_key: tuple[str, Any] | None = None
    human_lock = False
    day_kinds: dict[str, set[str]] = {}
    for e in items:
        if getattr(e, "precision", "minute") == "day" and getattr(e, "decided_by", "rule") != "human":
            day_kinds.setdefault(str(e.occurred_at)[:10], set()).add(e.kind)
        human = getattr(e, "decided_by", "rule") == "human"
        if human_lock and not human:
            continue  # решение человека не перебивается моделью и правилами
        before, side = res.state, getattr(e, "actor_side", None)
        new, note = before, ""
        kind = e.kind
        if kind == "proposed":
            new = "proposed" if before in ("open", "rejected", "withdrawn", "superseded", "proposed") else before
            if before in ("open", "rejected", "withdrawn", "superseded"):
                proposer_side, proposer_key = side, _actor_key(e)
        elif kind == "documented":
            if before in ("open", "rejected", "withdrawn", "superseded", "proposed", "documented"):
                new = "documented"
            if before in ("open", "rejected", "withdrawn", "superseded"):
                proposer_side, proposer_key = side, _actor_key(e)
        elif kind == "agreed":
            actor = _actor_key(e)
            if human or side == SIDE_CUSTOMER or (proposer_side and side and side != proposer_side):
                new = "agreed"
            elif internal and before != "implemented" and proposer_key and actor and actor != proposer_key:
                new = "agreed"  # внутренний проект: согласился коллега, не автор предложения; выполненное «ок» не понижает
            elif before in OPEN_OFFERS:
                note = ("согласие автора предложения: предложение не закрыто" if internal
                        else "согласие своей же стороны: предложение не закрыто")  # состояние то же
        elif kind == "rejected":
            if human or not (proposer_side and side and side == proposer_side):
                new = "rejected"
            else:
                new, note = "withdrawn", "отказ своей же стороны: предложение снято"
        elif kind in ("withdrawn", "superseded"):
            new = kind
        elif kind == "implemented":
            new = "implemented"
        elif kind == "reopened":
            new, proposer_side, proposer_key = "proposed", None, None
        if new != before or not res.trail or note:
            res.trail.append({"event_id": e.id, "kind": kind, "from": before, "to": new, "at": e.occurred_at, "note": note})
        if new != before:
            res.state, res.since, res.basis_event_id = new, str(e.occurred_at), e.id
        if human:
            human_lock, res.source = True, "human"
            res.state, res.since, res.basis_event_id = new, str(e.occurred_at), e.id
    clashes = [d for d, kinds in day_kinds.items() if len(kinds) > 1]
    if clashes:
        res.flags.append("ambiguous_order")
    return res


def events_of(db: Session, subject_id: int) -> list[DecisionEvent]:
    return list(db.scalars(select(DecisionEvent).where(DecisionEvent.subject_id == subject_id, DecisionEvent.is_active.is_(True))
                           .order_by(DecisionEvent.occurred_at, DecisionEvent.id)).all())


def is_internal_project(db: Session, project_id: int) -> bool:
    """Внутренний проект (только исполнитель): ``internal_project`` в настройках проекта."""
    from app.app.models.project import Project

    project = db.get(Project, project_id)
    return bool(project and project.settings().get("internal_project"))


def state_at(db: Session, subject_id: int, as_of: str | None = None, *, internal: bool | None = None) -> FoldResult:
    """Состояние на дату; ``internal`` не задан — берётся из настроек проекта предмета."""
    if internal is None:
        subject = db.get(Subject, subject_id)
        internal = bool(subject and is_internal_project(db, subject.project_id))
    return fold(events_of(db, subject_id), as_of=as_of, internal=internal)


def refresh_subject(db: Session, subject: Subject) -> FoldResult:
    """Пересчитать и записать состояние предмета по журналу (на сегодня)."""
    res = state_at(db, subject.id, internal=is_internal_project(db, subject.project_id))
    subject.state, subject.state_since, subject.state_source = res.state, res.since, res.source
    return res


def add_event(db: Session, subject: Subject, kind: str, occurred_at: str, *, precision: str = "minute", actor_label: str | None = None,
              actor_person_id: int | None = None, actor_side: str | None = None, artifact_id: int | None = None, anchor: str | None = None,
              quote: str | None = None, speech_act_id: int | None = None, supersedes_event_id: int | None = None, decided_by: str = "rule",
              confidence: float | None = None, note: str | None = None) -> DecisionEvent:
    from app.app.models.decision_memory import DECIDED_BY, EVENT_KINDS

    if kind not in EVENT_KINDS:
        raise ValueError(f"неизвестный вид события: {kind}")
    if decided_by not in DECIDED_BY:
        raise ValueError(f"неизвестный источник решения: {decided_by}")
    event = DecisionEvent(project_id=subject.project_id, subject_id=subject.id, kind=kind, actor_person_id=actor_person_id, actor_label=actor_label,
                          actor_side=actor_side, occurred_at=occurred_at, precision=precision, artifact_id=artifact_id, anchor=anchor, quote=quote,
                          speech_act_id=speech_act_id, supersedes_event_id=supersedes_event_id, decided_by=decided_by, confidence=confidence, note=note)
    db.add(event)
    db.flush()
    return event


def normalize_side(side: str | None) -> str | None:
    """Сторона реестра (executor / customer / unknown) -> то, с чем работает свёртка (customer / executor / None)."""
    return side if side in ("customer", "executor") else None


def occurred_for_act(act: Any, fallback_day: str | None) -> tuple[str, str]:
    """(момент, точность) события для речевого акта: время реплики, иначе день материала. Только дата (документ) — точность «день»."""
    said = getattr(act, "said_at", None)
    if said:
        return str(said), ("day" if len(str(said)) <= 10 else "minute")
    return (fallback_day or "0000-00-00"), "day"


def rebuild_events_from_acts(db: Session, project_id: int, assignments: dict[int, int], *, day_of_artifact: dict[int, str] | None = None) -> dict[str, int]:
    """Пересобрать из речевых актов события журнала проекта. ``assignments`` — {номер акта: номер предмета} (Э3).
    События человека и судьи не трогаются; прежние события от актов (правило и модель) заменяются. Возвращает счётчики."""
    from sqlalchemy import delete

    from app.app.models.speech_act import SpeechAct

    db.execute(delete(DecisionEvent).where(DecisionEvent.project_id == project_id, DecisionEvent.speech_act_id.is_not(None), DecisionEvent.decided_by != "human"))
    day_of_artifact = day_of_artifact or {}
    made = skipped = 0
    for act in db.scalars(select(SpeechAct).where(SpeechAct.project_id == project_id).order_by(SpeechAct.id)).all():
        kind = event_kind(act)
        subject_id = assignments.get(act.id)
        if kind is None or subject_id is None:
            skipped += 1
            continue
        subject = db.get(Subject, subject_id)
        if subject is None:
            skipped += 1
            continue
        when, precision = occurred_for_act(act, day_of_artifact.get(act.artifact_id))
        add_event(db, subject, kind, when, precision=precision, actor_label=act.author, actor_person_id=act.person_id, actor_side=normalize_side(act.side),
                  artifact_id=act.artifact_id, anchor=act.unit_ref, quote=act.quote, speech_act_id=act.id, decided_by="rule", note=act.about)
        made += 1
    db.flush()
    for subject in db.scalars(select(Subject).where(Subject.project_id == project_id)).all():
        refresh_subject(db, subject)
    return {"events": made, "skipped": skipped}
