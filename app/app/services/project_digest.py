"""Полоска «Что нового с прошлого захода» на странице проекта (docs/plans/PORTAL-ACCURACY-MANAGER-PLAN.md, Ш5).

Отсчёт (решение Р4-а): при открытии страницы, если человек не заходил в проект дольше ``SESSION_GAP``, прошлый заход
становится точкой отсчёта (``prev_seen_at``), текущий — ``seen_at``. Перезагрузка страницы в течение одного захода «новое»
не сбрасывает. Первый заход — полоски нет. «Скрыть» (``dismissed_at``) прячет полоску, пока не появится что-то новее.

Что новое, считается от материалов, обработанных после отсчёта (``jobs.finished_at`` первичной обработки): их пункты и
решения памяти по ним. События памяти считаются только по новым материалам — пересборка памяти перезаписывает их время.
Модель не вызывается; тяжёлый счёт «ждут вас» (контракт) страница подгружает отдельно, чтобы не замедлять её открытие.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.models.job import Job
from app.app.models.project_visit import ProjectVisit

SESSION_GAP = timedelta(minutes=30)
EXCHANGE_FILE = re.compile(r"^analysis_(reply|request|question)_\d+\.json$", re.IGNORECASE)
HIDDEN_RELEVANCE = ("superseded", "duplicate")
EVENT_LABELS = {"agreed": "согласовано", "implemented": "сделано", "rejected": "отклонено", "withdrawn": "снято"}


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """«1 задача», «3 задачи», «7 задач»."""
    n10, n100 = n % 10, n % 100
    form = forms[0] if n10 == 1 and n100 != 11 else forms[1] if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else forms[2]
    return f"{n} {form}"


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def touch_visit(db: Session, user_id: int, project_id: int, *, now: datetime | None = None) -> ProjectVisit:
    """Отметить заход; вернуть строку с отсчётом. Новый заход (пауза дольше SESSION_GAP) сдвигает отсчёт."""
    now = now or datetime.now(timezone.utc)
    visit = db.scalar(select(ProjectVisit).where(ProjectVisit.user_id == user_id, ProjectVisit.project_id == project_id))
    if visit is None:
        visit = ProjectVisit(user_id=user_id, project_id=project_id, seen_at=now, prev_seen_at=None)
        db.add(visit)
    elif now - _aware(visit.seen_at) > SESSION_GAP:
        visit.prev_seen_at = visit.seen_at
        visit.seen_at = now
    else:
        visit.seen_at = now  # тот же заход: отсчёт не двигается, только «был здесь»
    db.commit()
    return visit


def dismiss(db: Session, user_id: int, project_id: int, *, now: datetime | None = None) -> None:
    visit = touch_visit(db, user_id, project_id, now=now)
    visit.dismissed_at = now or datetime.now(timezone.utc)
    db.commit()


def _new_materials(db: Session, project_id: int, since: datetime) -> list[tuple[Artifact, datetime]]:
    rows = db.execute(
        select(Artifact, func.max(Job.finished_at))
        .join(Job, Job.artifact_id == Artifact.id)
        .where(Artifact.project_id == project_id, Artifact.status == "done", Job.kind == "process_artifact", Job.status == "done")
        .group_by(Artifact.id)
    ).all()
    return [(art, _aware(finished)) for art, finished in rows
            if finished is not None and _aware(finished) > since and not EXCHANGE_FILE.match(art.original_filename or "")]


def _item_counts(db: Session, artifact_ids: list[int]) -> dict[str, int]:
    counts = {"decision": 0, "task": 0, "open_question": 0}
    if not artifact_ids:
        return counts
    for ent in db.scalars(select(Entity).where(Entity.artifact_id.in_(artifact_ids), Entity.is_active.is_(True),
                                               Entity.entity_type.in_(tuple(counts)))).all():
        try:
            value = json.loads(ent.value_json or "{}")
        except ValueError:
            continue
        rel = value.get("relevance") if isinstance(value, dict) else None
        if isinstance(rel, dict) and rel.get("state") in HIDDEN_RELEVANCE and not rel.get("undone"):
            continue
        counts[ent.entity_type] += 1
    return counts


def _decision_changes(db: Session, settings: Any, project: Any, artifact_ids: list[int]) -> dict[str, int]:
    from app.app.models.decision_memory import DecisionEvent
    from app.app.services.memory_mode import mode_of

    if not artifact_ids or mode_of(settings, project) != "live":
        return {}
    rows = db.execute(
        select(DecisionEvent.kind, func.count(DecisionEvent.id))
        .where(DecisionEvent.project_id == project.id, DecisionEvent.is_active.is_(True), DecisionEvent.artifact_id.in_(artifact_ids),
               DecisionEvent.kind.in_(tuple(EVENT_LABELS)))
        .group_by(DecisionEvent.kind)
    ).all()
    return {kind: n for kind, n in rows if n}


def _agent_questions(db: Session, project_id: int, since: datetime) -> int:
    from app.app.models.agent_question import AgentQuestion

    rows = db.scalars(select(AgentQuestion.created_at).where(AgentQuestion.project_id == project_id, AgentQuestion.status != "answered")).all()
    return sum(1 for at in rows if at is not None and _aware(at) > since)


def _open_conflicts(db: Session, project_id: int) -> int:
    from app.app.models.entity import Link, Resolution

    return int(db.scalar(
        select(func.count(Link.id)).outerjoin(Resolution, Resolution.link_id == Link.id)
        .where(Link.project_id == project_id, Link.link_type == "POTENTIAL_CONFLICT", Resolution.id.is_(None))
    ) or 0)


def since_label(since: datetime) -> str:
    """«13 октября, 18:40» по местному времени портала."""
    from app.app.services.chat_export import RU_MONTHS
    from app.app.services.recorded_at import LOCAL_TZ

    local = since.astimezone(LOCAL_TZ)
    return f"{local.day} {RU_MONTHS[local.month - 1]}, {local:%H:%M}"


def build_digest(db: Session, settings: Any, project: Any, visit: ProjectVisit, *, role: str, today: date | None = None) -> dict[str, Any]:
    """Строки полоски. Пусто (``lines == []``) — полоску не показывать: первый заход, «Скрыть» или нечего сказать."""
    since = _aware(visit.prev_seen_at)
    out: dict[str, Any] = {"since": since.isoformat() if since else None, "since_label": since_label(since) if since else None,
                           "lines": [], "first_visit": since is None}
    if since is None:
        return out
    staff = role in ("admin", "manager")
    lines: list[dict[str, Any]] = []
    found = _new_materials(db, project.id, since)
    dismissed = _aware(visit.dismissed_at)
    if dismissed and dismissed > since and not any(finished > dismissed for _art, finished in found):
        return out  # «Скрыть» действует, пока после него не обработан новый материал
    materials = [art for art, _finished in found]
    if materials:
        counts = _item_counts(db, [a.id for a in materials])
        parts = [plural(n, forms) for n, forms in (
            (counts["decision"], ("решение или требование", "решения и требования", "решений и требований")),
            (counts["task"], ("задача", "задачи", "задач")),
            (counts["open_question"], ("вопрос к заказчику", "вопроса к заказчику", "вопросов к заказчику"))) if n]
        lines.append({"key": "materials", "count": len(materials), "target": "materials",
                      "text": f"Обработано новых материалов: {len(materials)}" + (f" — из них {', '.join(parts)}" if parts else "")})
        if staff:
            changes = _decision_changes(db, settings, project, [a.id for a in materials])
            if changes:
                lines.append({"key": "decisions", "count": sum(changes.values()), "target": "memory",
                              "text": "Решения по новым материалам: " + ", ".join(f"{EVENT_LABELS[k]} {n}" for k, n in changes.items())})
    if staff:
        asked = _agent_questions(db, project.id, since)
        if asked:
            lines.append({"key": "agent_questions", "count": asked, "target": "exchange", "text": f"Новых вопросов от агентов: {asked}"})
    from app.app.services.task_board import all_tasks

    tasks = all_tasks(db, project.id, today=today)
    overdue = sum(1 for t in tasks if t.get("overdue"))
    week = sum(1 for t in tasks if t.get("this_week"))
    if overdue or week:
        bits = [s for s in (f"просрочено {overdue}" if overdue else "", f"на этой неделе {week}" if week else "") if s]
        lines.append({"key": "tasks", "count": overdue + week, "target": "tasks", "view": "overdue" if overdue else "week",
                      "text": "Задачи: " + ", ".join(bits)})
    if role == "admin":
        conflicts = _open_conflicts(db, project.id)
        if conflicts:
            lines.append({"key": "conflicts", "count": conflicts, "target": "conflicts", "text": f"Нерешённых конфликтов: {conflicts}"})
    out["lines"] = lines
    return out


def digest_for_page(db: Session, settings: Any, project: Any, user: Any, role: str) -> dict[str, Any]:
    visit = touch_visit(db, user.id, project.id)
    return build_digest(db, settings, project, visit, role=role)
