"""Фоновая работа по проекту: прогресс, постановка, повтор ошибок, остановка (PLATFORM-FOUNDATION-PLAN, ПФ9).

Задачи проекта идут через очередь (``jobs``, вид ``auto_review``, область ``scope``): переизвлечение требований → проверка
требований → раскладка по темам → устаревшее и дубли → конфликты → итоговая выжимка. Здесь — то, чем эти задачи показывают ход:
сколько ждёт, что идёт сейчас, что закончилось за последние сутки, где ошибки и сколько осталось (по средней длительности
таких же задач). Перезапуск воркера задачи не теряет (``queue.requeue_orphaned_jobs``); упавшие повторяются кнопкой.
Работает на уже существующей очереди, новых таблиц нет. Ту же очередь будут использовать будущие пересчёты памяти решений.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.models.job import Job
from app.app.services.queue import KIND_AUTO_REVIEW, enqueue_auto_review

# Порядок этапов и подписи для людей
STAGES: tuple[tuple[str, str], ...] = (
    ("reextract", "Извлечение требований"),
    ("artifact", "Проверка требований моделью"),
    ("topics", "Раскладка по темам"),
    ("relevance", "Устаревшее и дубли"),
    ("conflicts", "Конфликты"),
    ("summary", "Итоговая выжимка"),
)
STAGE_LABEL = dict(STAGES)
RECENT_HOURS = 24
ERRORS_SHOWN = 10
DEFAULT_SECONDS = {"reextract": 150, "artifact": 90, "topics": 40, "relevance": 120, "conflicts": 60, "summary": 90}  # пока нет истории
FOLLOW_UP_SCOPES = ("artifact", "topics", "relevance", "conflicts")


class JobsError(ValueError):
    """Ошибка постановки; текст можно показывать пользователю."""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _scope(job: Job) -> str:
    try:
        return str(json.loads(job.payload_json or "{}").get("scope") or "")
    except ValueError:
        return ""


def _project_jobs(db: Session, project_id: int, *, since: datetime | None = None) -> list[tuple[Job, Artifact]]:
    q = (select(Job, Artifact).join(Artifact, Artifact.id == Job.artifact_id)
         .where(Artifact.project_id == project_id, Job.kind == KIND_AUTO_REVIEW))
    if since is not None:
        q = q.where((Job.status.in_(("queued", "running"))) | (Job.created_at >= since))
    return [(j, a) for j, a in db.execute(q.order_by(Job.id)).all()]


def _average_seconds(db: Session) -> dict[str, float]:
    """Средняя длительность последних завершённых задач каждого этапа (по всем проектам: модель и железо одни)."""
    out: dict[str, list[float]] = {}
    rows = db.execute(select(Job).where(Job.kind == KIND_AUTO_REVIEW, Job.status == "done", Job.started_at.is_not(None), Job.finished_at.is_not(None))
                      .order_by(Job.id.desc()).limit(300)).scalars().all()
    for job in rows:
        seconds = (job.finished_at - job.started_at).total_seconds()
        if 0 < seconds < 3600:
            out.setdefault(_scope(job), []).append(seconds)
    return {scope: sum(v) / len(v) for scope, v in out.items() if v}


def progress(db: Session, project_id: int) -> dict[str, Any]:
    """Ход фоновой работы: этапы со счётчиками, текущая задача, ошибки, оценка оставшегося времени."""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=RECENT_HOURS)
    jobs = _project_jobs(db, project_id, since=since)
    averages = _average_seconds(db)
    stages = []
    remaining = 0.0
    for scope, label in STAGES:
        mine = [(j, a) for j, a in jobs if _scope(j) == scope]
        counts = {s: sum(1 for j, _a in mine if j.status == s) for s in ("queued", "running", "done", "error")}
        left = counts["queued"] + counts["running"]
        remaining += left * averages.get(scope, DEFAULT_SECONDS[scope])
        if mine:
            stages.append({"scope": scope, "label": label, **counts})
    running = next(((j, a) for j, a in jobs if j.status == "running"), None)
    total_active = sum(1 for j, _a in jobs if j.status in ("queued", "running"))
    finished = sum(1 for j, _a in jobs if j.status in ("done", "error"))
    errors = [(j, a) for j, a in jobs if j.status == "error"]
    return {
        "active": total_active > 0,
        "pending": total_active,
        "finished_24h": finished,
        "percent": round(100 * finished / (finished + total_active)) if (finished + total_active) else None,
        "estimate_minutes": round(remaining / 60) if total_active else 0,
        "stages": stages,
        "current": {"stage": STAGE_LABEL.get(_scope(running[0]), _scope(running[0])), "material": running[1].original_filename,
                    "since": _iso(running[0].started_at)} if running else None,
        "errors": [{"id": j.id, "stage": STAGE_LABEL.get(_scope(j), _scope(j)), "material": a.original_filename,
                    "message": (j.error_message or "")[:300], "at": _iso(j.finished_at)} for j, a in errors[-ERRORS_SHOWN:]],
        "errors_total": len(errors),
    }


def _materials(db: Session, project_id: int, artifact_ids: list[int] | None) -> list[Artifact]:
    from app.app.services.requirements_reextract import project_materials

    arts = project_materials(db, project_id)
    if artifact_ids:
        wanted = set(artifact_ids)
        arts = [a for a in arts if a.id in wanted]
        if not arts:
            raise JobsError("Выбранные материалы не найдены или ещё не готовы")
    if not arts:
        raise JobsError("В проекте нет готовых материалов")
    return arts


def _mode(settings: Settings, db: Session, project_id: int) -> str:
    from app.app.models.project import Project
    from app.app.services.auto_review import MODE_LIVE, MODE_SHADOW, review_mode

    mode = review_mode(settings, db.get(Project, project_id))
    if mode not in (MODE_SHADOW, MODE_LIVE):
        raise JobsError("Автопроверка выключена (режим off): включите shadow или live")
    return mode


def queue_review(db: Session, settings: Settings, project_id: int, *, artifact_ids: list[int] | None = None) -> dict[str, Any]:
    """«Перепроверить»: все проверки без повторного извлечения — требования, темы, устаревшее, конфликты, затем выжимка
    (то, что делал ``scripts/review_project.py`` без ``--reextract``). Можно по выбранным материалам."""
    arts = _materials(db, project_id, artifact_ids)
    mode = _mode(settings, db, project_id)
    queued = 0
    for art in arts:
        for scope in FOLLOW_UP_SCOPES:
            queued += enqueue_auto_review(db, art.id, scope=scope, mode=mode) is not None
    queued += enqueue_auto_review(db, arts[-1].id, scope="summary", mode=mode) is not None  # выжимка — за проверками, один раз
    return {"project_id": project_id, "materials": len(arts), "jobs_queued": queued, "mode": mode}


def queue_reextract(db: Session, settings: Settings, project_id: int, *, artifact_ids: list[int] | None = None) -> dict[str, Any]:
    """«Переизвлечь»: все материалы проекта (как раньше) или только выбранные. У выбранных темы пересобираются сами,
    общие темы проекта не стираются."""
    from app.app.models.entity import Entity
    from app.app.services.requirements_reextract import SCOPE, queue_project_reextract

    if not artifact_ids:
        _materials(db, project_id, None)
        return queue_project_reextract(db, settings, project_id)
    arts = _materials(db, project_id, artifact_ids)
    mode = _mode(settings, db, project_id)
    db.execute(update(Entity).where(Entity.artifact_id.in_([a.id for a in arts])).values(topic_id=None))
    db.commit()
    queued = sum(enqueue_auto_review(db, art.id, scope=SCOPE, mode=mode) is not None for art in arts)
    return {"project_id": project_id, "materials": len(arts), "jobs_queued": queued, "mode": mode}


def retry_errors(db: Session, project_id: int) -> int:
    """Вернуть упавшие задачи проекта в очередь (попытки обнуляются). Вернуть, сколько задач возвращено."""
    ids = [j.id for j, _a in _project_jobs(db, project_id) if j.status == "error"]
    if not ids:
        return 0
    db.execute(update(Job).where(Job.id.in_(ids)).values(status="queued", attempts=0, started_at=None, finished_at=None, error_message=None))
    db.commit()
    return len(ids)


def cancel_pending(db: Session, project_id: int) -> int:
    """Убрать из очереди ждущие задачи проекта. Идущая сейчас доработает: воркер её не прерывает."""
    ids = [j.id for j, _a in _project_jobs(db, project_id) if j.status == "queued"]
    if not ids:
        return 0
    db.query(Job).filter(Job.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return len(ids)

