"""Кнопки портала, которым нужна модель, — задачи очереди ``kind=project_task``: пересборка выжимки проекта,
переизвлечение задач и сроков, черновик карточки проекта, пересборка выжимки созвона или документа.

Раньше их выполнял процесс API прямо в запросе, мимо очереди воркера. Теперь у API своя полоса модели только для чата
(docs/plans/LLM-LANES-PLAN.md), а эти задачи идут фоновой полосой строго по очереди.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.job import Job
from app.app.services.queue import (
    PROJECT_TASK_CARD,
    PROJECT_TASK_ENTITIES,
    PROJECT_TASK_SUMMARY,
    PROJECT_TASK_TZ,
    project_task_payload,
)

logger = logging.getLogger("primaryanalitics.project_jobs")


def _activity(db: Session, data: dict[str, Any], *, block: str, action: str, title: str, artifact: Artifact | None = None) -> None:
    from app.app.services.user_activity import log_activity

    user_id = data.get("user_id")
    if not user_id:
        return
    log_activity(
        db,
        user_id=int(user_id),
        project_id=int(data["project_id"]),
        block=block,
        action=action,
        title=title,
        target_kind="artifact" if artifact else "project",
        target_id=artifact.id if artifact else int(data["project_id"]),
    )


def run_project_task(db: Session, settings: Settings, job: Job, artifact: Artifact) -> dict[str, Any]:
    from app.app.services.user_activity import ACTION_REBUILD, ACTION_REEXTRACT, BLOCK_KNOWLEDGE, BLOCK_REPORT, BLOCK_TZ

    data = project_task_payload(job)
    task = str(data.get("task") or "")
    project_id = int(data.get("project_id") or artifact.project_id)
    if task == PROJECT_TASK_TZ:
        from app.app.services.tz import rebuild_tz_fast

        path = rebuild_tz_fast(db, settings, project_id, manual=True)
        _activity(db, data, block=BLOCK_TZ, action=ACTION_REBUILD, title="Пересобрал выжимку проекта")
        result: dict[str, Any] = {"path": str(path)}
    elif task == PROJECT_TASK_ENTITIES:
        from app.app.services.entities import reextract_project_entities

        result = reextract_project_entities(db, settings, project_id)
        _activity(db, data, block=BLOCK_KNOWLEDGE, action=ACTION_REEXTRACT, title="Переизвлёк сущности")
    elif task == PROJECT_TASK_CARD:
        from app.app.services.extraction_context import build_project_card_draft, save_project_card

        draft = build_project_card_draft(db, settings, project_id)
        if not draft:
            raise RuntimeError("Нет материалов для карточки: нужна выжимка проекта или выжимки созвонов")
        save_project_card(settings, project_id, draft, source="model")
        _activity(db, data, block=BLOCK_KNOWLEDGE, action=ACTION_REBUILD, title="Пересобрал черновик карточки проекта")
        result = {"chars": len(draft)}
    elif task == PROJECT_TASK_SUMMARY:
        from app.app.services.speaker_profiles import rebuild_artifact_call_summary

        result = rebuild_artifact_call_summary(db, project_id=project_id, artifact_id=artifact.artifact_id)
        title = "Пересобрал выжимку созвона" if artifact.file_type == "audio" else "Пересобрал выжимку документа"
        _activity(db, data, block=BLOCK_REPORT, action=ACTION_REBUILD, title=title, artifact=artifact)
    else:
        raise ValueError(f"неизвестная задача проекта: {task!r}")
    db.commit()
    log_event(logger, "project_task.done", task=task, project=project_id, artifact=artifact.artifact_id)
    return result
