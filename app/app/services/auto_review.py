"""Автопроверка локальной моделью: режим (off / shadow / live), запись вердикта модели, отмена человеком, контрольная выборка.

Решение модели хранится в ``Entity.value_json.model_review`` и всегда отличимо от человеческого (``human_review``):
``needs_review`` по-прежнему значит «человек не смотрел». Человек главнее модели — сущность с ``human_review``
модель не трогает. Ничего не удаляется: отказ модели в режиме live выключает сущность (``is_active=False``),
отмена человеком возвращает её. План: docs/plans/AUTO-REVIEW-PLAN.md.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.entity import Entity

logger = logging.getLogger("skkkk.review")

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_LIVE = "live"
MODES = (MODE_OFF, MODE_SHADOW, MODE_LIVE)
VERDICTS = ("confirm", "reject", "unsure")


def review_mode(settings: Settings, project: Any | None = None) -> str:
    """Режим проекта: настройка проекта ``auto_review_mode`` главнее общей из .env; неизвестное значение → off."""
    mode = settings.auto_review_mode
    if project is not None:
        try:
            mode = (project.settings() or {}).get("auto_review_mode") or mode
        except (AttributeError, ValueError, TypeError):
            pass
    mode = str(mode or "").strip().lower()
    return mode if mode in MODES else MODE_OFF


def is_audit(entity_id: int, settings: Settings) -> bool:
    """Контрольная выборка: каждое N-е авторешение (детерминированно по id) дополнительно идёт человеку."""
    every = max(1, int(settings.auto_review_audit_every or 0) or 1)
    return every > 1 and entity_id % every == 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(entity: Entity) -> dict[str, Any]:
    try:
        value = json.loads(entity.value_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {"text": str(value)}


def model_review_of(entity: Entity) -> dict[str, Any] | None:
    review = _load(entity).get("model_review")
    return review if isinstance(review, dict) else None


def record_model_review(
    entity: Entity,
    settings: Settings,
    *,
    verdict: str,
    reason: str,
    confidence: float,
    mode: str,
    model: str,
    prompt_version: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Записать вердикт модели. Вернуть запись или None, если сущность уже решил человек или режим off.

    live + reject → ``is_active=False`` (с пометкой ``deactivated``, чтобы «Вернуть» знало, что возвращать).
    shadow — только запись, без последствий: по ней меряется согласие модели с человеком.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict: один из {', '.join(VERDICTS)}")
    if mode not in (MODE_SHADOW, MODE_LIVE):
        return None
    value = _load(entity)
    if value.get("human_review"):
        return None
    review: dict[str, Any] = {
        "verdict": verdict,
        "reason": (reason or "").strip()[:300],
        "confidence": round(float(confidence), 3),
        "mode": mode,
        "model": model,
        "prompt_version": prompt_version,
        "at": _now(),
        "audit": is_audit(entity.id, settings) if entity.id is not None else False,
    }
    if extra:
        review.update(extra)
    if mode == MODE_LIVE and verdict == "reject" and entity.is_active:
        entity.is_active = False
        review["deactivated"] = True
    value["model_review"] = review
    entity.value_json = json.dumps(value, ensure_ascii=False)
    log_event(
        logger,
        "review.verdict",
        entity=entity.id,
        type=entity.entity_type,
        verdict=verdict,
        confidence=review["confidence"],
        mode=mode,
        audit=review["audit"],
    )
    return review


# Замер 2026-09-24 (scripts/eval_auto_review.py, Qwen 27B): ~2.6 с на требование, ~2.8 с на конфликт.
SECONDS_PER_REQUIREMENT = 2.6
SECONDS_PER_CONFLICT = 2.8


class BacklogError(ValueError):
    pass


def queue_backlog(db: Any, settings: Settings, project_id: int, *, scope: str = "all", mode: str | None = None) -> dict[str, Any]:
    """Поставить фоновую проверку всего накопленного в проекте (этап 5): по задаче на готовый артефакт и область.

    Только если запущенный воркер понимает задачи ``auto_review`` — иначе старый воркер переобработал бы созвоны.
    Режим по умолчанию — режим проекта; ``live`` повторно модель не зовёт там, где уже есть теневой вердикт.
    """
    from sqlalchemy import select

    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.services.queue import KIND_AUTO_REVIEW, enqueue_auto_review, running_worker_kinds

    if scope not in ("all", "requirements", "conflicts"):
        raise BacklogError("Неизвестная область проверки: нужна all, requirements или conflicts")
    project = db.get(Project, project_id)
    mode = mode or review_mode(settings, project)
    if mode not in (MODE_SHADOW, MODE_LIVE):
        raise BacklogError("Автопроверка выключена (режим off) — включите shadow или live")
    if KIND_AUTO_REVIEW not in running_worker_kinds(settings.data_dir):
        raise BacklogError("Запущен воркер старой версии: перезапустите сервер, иначе проверка переобработает созвоны")
    arts = db.scalars(select(Artifact).where(Artifact.project_id == project_id, Artifact.status == "done").order_by(Artifact.id)).all()
    from app.app.services.review_conflicts import pending_links
    from app.app.services.review_requirements import _pending

    requirements = sum(len(_pending(db, art)) for art in arts) if scope in ("all", "requirements") else 0
    conflicts = len(pending_links(db, project_id=project_id)) if scope in ("all", "conflicts") else 0
    queued = 0
    for art in arts:
        if scope in ("all", "requirements") and enqueue_auto_review(db, art.id, scope="artifact", mode=mode):
            queued += 1
        if scope in ("all", "conflicts") and enqueue_auto_review(db, art.id, scope="conflicts", mode=mode):
            queued += 1
    estimate = requirements * SECONDS_PER_REQUIREMENT + conflicts * SECONDS_PER_CONFLICT
    log_event(logger, "review.backlog", project_id=project_id, scope=scope, mode=mode, jobs=queued,
              requirements=requirements, conflicts=conflicts)
    return {"project_id": project_id, "scope": scope, "mode": mode, "artifacts": len(arts), "jobs_queued": queued,
            "requirements": requirements, "conflicts": conflicts, "estimate_minutes": round(estimate / 60, 1)}


def verification_for(needs_review: bool, value: dict[str, Any]) -> str:
    """Проверка требования для контракта: human_confirmed (человек) → model_confirmed (модель, live) → extracted.

    Теневой вердикт (shadow) и отменённый человеком проверкой не считаются.
    """
    if not needs_review:
        return "human_confirmed"
    review = value.get("model_review") if isinstance(value, dict) else None
    if (
        isinstance(review, dict)
        and review.get("mode") == MODE_LIVE
        and review.get("verdict") == "confirm"
        and not review.get("undone")
    ):
        return "model_confirmed"
    return "extracted"


def public_model_review(value: dict[str, Any]) -> dict[str, Any] | None:
    """Вердикт модели для контракта / UI без служебных полей."""
    review = value.get("model_review") if isinstance(value, dict) else None
    if not isinstance(review, dict):
        return None
    keys = ("verdict", "reason", "confidence", "mode", "at", "prompt_version", "audit", "reject_kind", "undone", "deactivated")
    return {k: review.get(k) for k in keys if review.get(k) is not None}


def needs_human(verification: str, model_review: dict[str, Any] | None) -> bool:
    """Ждёт ли требование человека: не проверено никем, модель не уверена/в тени, или попало в контрольную выборку."""
    if verification == "human_confirmed":
        return False
    if verification == "model_confirmed":
        return bool(model_review and model_review.get("audit"))
    return True


def undo_model_review(entity: Entity, *, user_name: str, user_id: int | None) -> dict[str, Any]:
    """«Вернуть»: отменить решение модели. Выключенную моделью сущность — включить; отмена пишется как решение человека."""
    value = _load(entity)
    review = value.get("model_review")
    if not isinstance(review, dict):
        raise ValueError("у сущности нет решения модели")
    if review.get("undone"):
        raise ValueError("решение модели уже отменено")
    if review.get("deactivated"):
        entity.is_active = True
    at = _now()
    review["undone"] = {"by": user_name, "user_id": user_id, "at": at}
    value["model_review"] = review
    value["human_review"] = {"by": user_name, "user_id": user_id, "at": at, "action": "undo_model"}
    entity.value_json = json.dumps(value, ensure_ascii=False)
    log_event(logger, "review.undo", entity=entity.id, verdict=review.get("verdict"), by=user_name)
    return review
