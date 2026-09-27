"""Переизвлечение требований проекта по новой схеме (docs/plans/REQUIREMENTS-PLAN.md, этап 9; решение А10).

Каждый готовый материал — фоновая задача очереди (``auto_review``, scope ``reextract``): извлечение v2 с контекстом
проекта (карточка, оглавление созвона, темы), перенос ручных решений на новые пункты (``entities.persist_entities``),
пересев графа. Затем материал ставит свои проверки: требования → темы → актуальность → конфликты. Проверки идут в
очереди после всех переизвлечений проекта (FIFO), актуальность ждёт, пока проект не разобран целиком
(``review_relevance._project_busy``). Расшифровку и разбор документов заново не делаем — текст уже есть.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity, Topic

logger = logging.getLogger("skkkk.review")

SCOPE = "reextract"
FOLLOW_UP_SCOPES = ("artifact", "topics", "relevance", "conflicts")
# Файлы обмена с внешним агентом — не материалы проекта (их и на странице проекта нет в списке материалов)
EXCHANGE_FILE = re.compile(r"^analysis_(reply|request|question)_\d+\.json$", re.IGNORECASE)


def project_materials(db: Session, project_id: int) -> list[Artifact]:
    arts = db.scalars(select(Artifact).where(Artifact.project_id == project_id, Artifact.status == "done").order_by(Artifact.id)).all()
    return [a for a in arts if not EXCHANGE_FILE.match(a.original_filename or "")]


def human_decisions_count(db: Session, project_id: int) -> int:
    rows = db.scalars(
        select(Entity.value_json).join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.entity_type.in_(("decision", "task", "open_question")),
               Entity.value_json.like('%"human_review"%'))
    ).all()
    count = 0
    for raw in rows:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        count += bool(isinstance(value, dict) and value.get("human_review"))
    return count


def _mode(settings: Settings, project: Any) -> str:
    from app.app.services.auto_review import MODE_SHADOW, review_mode

    mode = review_mode(settings, project)
    return mode if mode in ("shadow", "live") else MODE_SHADOW


def queue_project_reextract(db: Session, settings: Settings, project_id: int) -> dict[str, Any]:
    """Поставить переизвлечение всех готовых материалов проекта. Темы проекта собираются заново."""
    from app.app.models.project import Project
    from app.app.services.queue import enqueue_auto_review

    project = db.get(Project, project_id)
    arts = project_materials(db, project_id)
    mode = _mode(settings, project)
    db.execute(update(Entity).where(Entity.artifact_id.in_([a.id for a in arts] or [0])).values(topic_id=None))
    db.query(Topic).filter(Topic.project_id == project_id).delete(synchronize_session=False)
    db.commit()
    queued = 0
    for art in arts:
        queued += enqueue_auto_review(db, art.id, scope=SCOPE, mode=mode) is not None
    summary = {"project_id": project_id, "materials": len(arts), "jobs_queued": queued, "mode": mode,
               "human_decisions": human_decisions_count(db, project_id)}
    log_event(logger, "reextract.queued", **summary)
    return summary


def reextract_for_review(db: Session, settings: Settings, artifact: Artifact, mode: str) -> dict[str, Any]:
    """Исполнитель очереди (scope ``reextract``): извлечение v2 → граф → постановка проверок этого материала."""
    from app.app.services import contract as ctr
    from app.app.services.entities import reextract_artifact_entities
    from app.app.services.graph import seed_graph
    from app.app.services.queue import enqueue_auto_review
    from app.app.services.storage import artifact_knowledge_text

    started = time.monotonic()
    result = reextract_artifact_entities(db, settings, artifact)
    if result.get("ok") and not settings.dry_run:
        seed_graph(db, settings, artifact, artifact_knowledge_text(settings, artifact) or artifact.transcription_text or "")
        db.commit()
    ctr._cache.pop(artifact.project_id, None)
    for scope in FOLLOW_UP_SCOPES:
        enqueue_auto_review(db, artifact.id, scope=scope, mode=mode)
    result["seconds"] = round(time.monotonic() - started, 1)
    log_event(logger, "reextract.done", artifact=artifact.artifact_id, ok=result.get("ok"), tasks=result.get("tasks"),
              seconds=result["seconds"])
    return result
