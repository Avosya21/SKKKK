"""Память решений в очереди воркера: новый материал дописывается в память сам (docs/plans/DECISION-MEMORY-STAGES-6-8.md, Э8).

Две области задач ``auto_review``:
- ``memory`` — на материал: речевые акты его реплик (``speech_acts.extract_artifact``);
- ``memory_judge`` — на проект, после актов всех материалов: раскладка новых актов по предметам, склейка дублей, события журнала,
  итоги судьи только у предметов с изменившейся лентой, сравнение с прежним контуром и, если проект готов, автопереход shadow -> live.
Если за ней в очереди проекта ждёт ещё такая же задача или акты материала — она откладывается: партия собирается один раз.
Память строится только для проектов в режиме shadow или live (``memory_mode``); в off задачи пропускаются без вызова модели.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.job import Job
from app.app.services import memory_mode

logger = logging.getLogger("primaryanalitics.memory_pipeline")

SCOPE_ACTS = "memory"
SCOPE_JUDGE = "memory_judge"
SCOPES = (SCOPE_ACTS, SCOPE_JUDGE)


def project_enabled(settings: Settings, artifact: Artifact) -> bool:
    return memory_mode.mode_of(settings, artifact.project) != "off"


def _waiting(db: Session, project_id: int, scopes: tuple[str, ...], *, exclude_artifact: int | None = None, queued_only: bool = False) -> int:
    """Сколько задач этих областей у проекта ждёт (и идёт, если не ``queued_only``), кроме задач указанного материала."""
    stmt = select(func.count()).select_from(Job).join(Artifact, Artifact.id == Job.artifact_id).where(Artifact.project_id == project_id)
    stmt = stmt.where(Job.status == "queued") if queued_only else stmt.where(Job.status.in_(("queued", "running")))
    if exclude_artifact is not None:
        stmt = stmt.where(Artifact.id != exclude_artifact)
    from sqlalchemy import or_

    stmt = stmt.where(or_(*[Job.payload_json.like(f'%"scope": "{s}"%') for s in scopes]))
    return int(db.scalar(stmt) or 0)


def building(db: Session, project_id: int) -> int:
    """Сколько задач памяти проекта ещё ждёт или идёт: карточка показывает ход, повторное «Построить» в это время не принимается."""
    return _waiting(db, project_id, SCOPES)


def enqueue_for_artifact(db: Session, settings: Settings, artifact: Artifact, mode: str) -> list[str]:
    """Поставить задачи памяти нового материала (после проверок и выжимки). Возвращает поставленные области."""
    from app.app.services.queue import enqueue_auto_review

    if not project_enabled(settings, artifact):
        return []
    queued = []
    for scope in SCOPES:
        if enqueue_auto_review(db, artifact.id, scope=scope, mode=mode) is not None:
            queued.append(scope)
    return queued


def queue_project(db: Session, settings: Settings, project_id: int) -> dict[str, Any]:
    """«Построить память»: акты всех готовых материалов проекта и итог проекта. Режим off заменяется на shadow (память нужна)."""
    from app.app.models.project import Project
    from app.app.services.auto_review import MODE_LIVE, MODE_SHADOW, review_mode
    from app.app.services.queue import enqueue_auto_review
    from app.app.services.requirements_reextract import project_materials

    if building(db, project_id):
        raise ValueError("Память уже строится: дождитесь окончания, она появится в карточке сама")
    project = db.get(Project, project_id)
    if memory_mode.mode_of(settings, project) == "off":
        memory_mode.set_mode(db, project, "shadow")
    arts = project_materials(db, project_id)
    if not arts:
        raise ValueError("В проекте нет готовых материалов")
    mode = review_mode(settings, project)
    mode = mode if mode in (MODE_SHADOW, MODE_LIVE) else MODE_SHADOW
    queued = sum(enqueue_auto_review(db, a.id, scope=SCOPE_ACTS, mode=mode) is not None for a in arts)
    queued += enqueue_auto_review(db, arts[-1].id, scope=SCOPE_JUDGE, mode=mode) is not None
    return {"project_id": project_id, "materials": len(arts), "jobs_queued": queued}


def retire_empty_subjects(db: Session, project_id: int) -> int:
    """Предметы без актов (материал удалён или переразобран) снимаются с показа; решения человека остаются в журнале."""
    from app.app.models.decision_memory import Subject
    from app.app.models.speech_act import SpeechAct

    alive = set(db.scalars(select(SpeechAct.subject_id).where(SpeechAct.project_id == project_id, SpeechAct.subject_id.is_not(None))).all())
    n = 0
    for s in db.scalars(select(Subject).where(Subject.project_id == project_id, Subject.is_active.is_(True))).all():
        if s.id not in alive:
            s.is_active = False
            n += 1
    db.commit()
    return n


def memory_for_review(db: Session, settings: Settings, artifact: Artifact, mode: str) -> dict[str, Any]:
    """Исполнитель области ``memory``: акты реплик материала."""
    from app.app.services import speech_acts

    if not project_enabled(settings, artifact):
        return {"skipped": "память решений выключена"}
    started = time.monotonic()
    stats = speech_acts.extract_artifact(db, settings, artifact)
    stats["seconds"] = round(time.monotonic() - started, 1)
    return stats


def build_project(db: Session, settings: Settings, project_id: int, *, ask: Any = None, embed: Any = None, progress: Any = None) -> dict[str, Any]:
    """Дописать память проекта по уже готовым актам: предметы только для новых актов, склейка, события, итоги изменённых предметов,
    сравнение с прежним контуром, автопереход режима. Без ручных шагов; повторный вызов без новых актов почти ничего не делает."""
    from app.app.models.project import Project
    from app.app.services import memory_compare, subject_judge, subject_windows, subjects

    out: dict[str, Any] = {"retired": retire_empty_subjects(db, project_id)}
    out["subjects"] = subject_windows.assign(db, settings, project_id, rebuild=False, ask=ask, embed=embed)
    out["merge"] = subject_windows.merge_similar(db, settings, project_id, ask=ask)
    snapshot = subject_judge.snapshot_judged(db, project_id)
    out["events"] = subjects.build_events(db, project_id)
    out["restored"] = subject_judge.restore_unchanged(db, snapshot)
    out["judge"] = subject_judge.judge_project(db, settings, project_id, ask=ask, progress=progress, only_pending=True)
    out["compare"] = memory_compare.compare_project(db, settings, project_id, ask=ask)
    project = db.get(Project, project_id)
    out["promoted"] = memory_mode.maybe_promote(db, settings, project) if project is not None else None
    return out


def memory_judge_for_review(db: Session, settings: Settings, artifact: Artifact, mode: str) -> dict[str, Any]:
    """Исполнитель области ``memory_judge``: итог по проекту после актов всех его материалов."""
    if not project_enabled(settings, artifact):
        return {"skipped": "память решений выключена"}
    pending_acts = _waiting(db, artifact.project_id, (SCOPE_ACTS,), exclude_artifact=artifact.id)
    later_judge = _waiting(db, artifact.project_id, (SCOPE_JUDGE,), exclude_artifact=artifact.id, queued_only=True)
    if pending_acts or later_judge:
        log_event(logger, "memory.deferred", project=artifact.project_id, artifact=artifact.artifact_id, acts_waiting=pending_acts, judge_waiting=later_judge)
        return {"deferred": pending_acts + later_judge}
    started = time.monotonic()
    out = build_project(db, settings, artifact.project_id)
    out["seconds"] = round(time.monotonic() - started, 1)
    log_event(logger, "memory.built", project=artifact.project_id, artifact=artifact.artifact_id, seconds=out["seconds"])
    return out
