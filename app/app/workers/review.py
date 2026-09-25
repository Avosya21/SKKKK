"""Фоновая автопроверка артефакта локальной моделью (задачи очереди ``kind=auto_review``).

Исполнители регистрируются по области (``scope``): ``artifact`` — требования (этап 2 плана), ``conflicts`` — конфликты
(этап 4). Ошибка автопроверки не трогает статус артефакта: он уже ``done``. План: docs/plans/AUTO-REVIEW-PLAN.md.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.job import Job
from app.app.services.auto_review import MODE_LIVE, MODE_SHADOW

logger = logging.getLogger("skkkk.review")

Reviewer = Callable[[Session, Settings, Artifact, str], dict[str, Any]]
REVIEWERS: dict[str, list[Reviewer]] = {"artifact": [], "conflicts": []}


def register_reviewer(scope: str, fn: Reviewer) -> Reviewer:
    if scope not in REVIEWERS:
        raise ValueError(f"неизвестная область автопроверки: {scope}")
    if fn not in REVIEWERS[scope]:
        REVIEWERS[scope].append(fn)
    return fn


def _register_defaults() -> None:
    from app.app.services.review_conflicts import review_artifact_conflicts
    from app.app.services.review_requirements import review_artifact_requirements

    register_reviewer("artifact", review_artifact_requirements)
    register_reviewer("conflicts", review_artifact_conflicts)


_register_defaults()


def _payload(job: Job) -> dict[str, Any]:
    try:
        data = json.loads(job.payload_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def refresh_knowledge(db: Session, settings: Settings, artifact: Artifact) -> None:
    """После имени голоса у готового созвона: чанки с новыми именами (rebind внутри index_artifact) и выжимка созвона."""
    from app.app.services.gpu_phase import prepare_for_llm
    from app.app.services.indexing import index_artifact
    from app.app.services.pipeline_audio import run_call_summary
    from app.app.services.storage import artifact_knowledge_text

    started = time.monotonic()
    artifact_key = artifact.artifact_id
    text = artifact_knowledge_text(settings, artifact)
    chunks = index_artifact(db, settings, artifact, text) if text else 0
    db.commit()
    if artifact.file_type == "audio":
        prepare_for_llm(settings.lmstudio_model, settings=settings)
        run_call_summary(settings, artifact)
    log_event(logger, "knowledge.refreshed", artifact=artifact_key, chunks=chunks, seconds=round(time.monotonic() - started, 1))


def process_review(db: Session, settings: Settings, job: Job, artifact: Artifact) -> dict[str, Any]:
    """Выполнить автопроверку по задаче. Вернуть сводку по исполнителям."""
    payload = _payload(job)
    scope = str(payload.get("scope") or "")
    mode = str(payload.get("mode") or "")
    if scope not in REVIEWERS:
        raise ValueError(f"неизвестная область автопроверки: {scope!r}")
    if mode not in (MODE_SHADOW, MODE_LIVE):
        raise ValueError(f"неизвестный режим автопроверки: {mode!r}")
    started = time.monotonic()
    summary: dict[str, Any] = {"scope": scope, "mode": mode, "reviewers": {}}
    reviewers = REVIEWERS[scope]
    if not reviewers:
        log_event(logger, "review.skipped", artifact=artifact.artifact_id, scope=scope, reason="no reviewers")
        summary["skipped"] = "no reviewers"
        return summary
    for fn in reviewers:
        summary["reviewers"][fn.__name__] = fn(db, settings, artifact, mode)
        db.commit()
    log_event(
        logger,
        "review.done",
        artifact=artifact.artifact_id,
        scope=scope,
        mode=mode,
        seconds=round(time.monotonic() - started, 1),
        result=json.dumps(summary["reviewers"], ensure_ascii=False)[:500],
    )
    return summary
