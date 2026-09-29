"""Переизвлечение требований проекта по новой схеме (docs/plans/REQUIREMENTS-PLAN.md, этап 9; решение А10).

Каждый готовый материал — фоновая задача очереди (``auto_review``, scope ``reextract``): извлечение v2 с контекстом
проекта (карточка, оглавление созвона, темы), перенос ручных решений на новые пункты (``entities.persist_entities``),
пересев графа. Затем материал ставит свои проверки: требования → темы → актуальность → конфликты. Проверки идут в
очереди после всех переизвлечений проекта (FIFO), актуальность ждёт, пока проект не разобран целиком
(``review_relevance._project_busy``). Последний материал проекта ставит за своими проверками итоговую выжимку
(scope ``summary``) — она собирается, когда устаревшее уже выключено. Расшифровку и разбор документов заново не
делаем — текст уже есть.
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
SUMMARY_SCOPE = "summary"  # итоговая выжимка проекта — за проверками всех материалов (переизвлечение и новые материалы)
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
    """Исполнитель очереди (scope ``reextract``): извлечение v2 → граф → постановка проверок этого материала.

    Выжимку проекта здесь не собираем: актуальность и конфликты выключают устаревшее позже, и собранная сразу выжимка
    сохраняла заменённые пункты (2026-09-28, проект 34: «сводка по опозданиям в Битрикс24», отменённая перепиской).
    Последнее переизвлечение проекта ставит её отдельной задачей за своими проверками (``_queue_summary_if_last``).
    """
    from app.app.services import contract as ctr
    from app.app.services.entities import reextract_artifact_entities
    from app.app.services.graph import seed_graph
    from app.app.services.queue import enqueue_auto_review
    from app.app.services.storage import artifact_knowledge_text

    started = time.monotonic()
    try:
        result = reextract_artifact_entities(db, settings, artifact)
        if result.get("ok") and not settings.dry_run:
            seed_graph(db, settings, artifact, artifact_knowledge_text(settings, artifact) or artifact.transcription_text or "")
            db.commit()
        ctr._cache.pop(artifact.project_id, None)
        for scope in FOLLOW_UP_SCOPES:
            enqueue_auto_review(db, artifact.id, scope=scope, mode=mode)
    except Exception:
        db.rollback()
        _queue_summary_if_last(db, artifact, mode)  # упал последний материал — выжимка по остальным всё равно нужна
        raise
    _queue_summary_if_last(db, artifact, mode)
    result["seconds"] = round(time.monotonic() - started, 1)
    log_event(logger, "reextract.done", artifact=artifact.artifact_id, ok=result.get("ok"), tasks=result.get("tasks"),
              seconds=result["seconds"])
    return result


def _queue_summary_if_last(db: Session, artifact: Artifact, mode: str) -> None:
    """Последнее переизвлечение проекта ставит итоговую выжимку в конец очереди. Воркер один, задачи идут по порядку
    постановки, а проверки всех материалов уже стоят раньше — выжимка соберётся после конфликтов последнего материала."""
    from app.app.services.queue import enqueue_auto_review

    if not _reextract_left(db, artifact):
        enqueue_auto_review(db, artifact.id, scope=SUMMARY_SCOPE, mode=mode)


def summary_for_review(db: Session, settings: Settings, artifact: Artifact, mode: str) -> dict[str, Any]:
    """Исполнитель очереди (scope ``summary``): итоговая выжимка проекта по пунктам, оставшимся в силе после проверок
    переизвлечения или нового материала (как ``scripts/review_project.py``: выжимка — в конце, после конфликтов).

    Новый материал ставит её за своими проверками (``pipeline_knowledge._enqueue_auto_review``). Если в очереди ждёт ещё
    одна выжимка проекта — она дальше, за проверками следующих материалов, — эта пропускается без вызова модели: партия
    собирается один раз (решение владельца 2026-09-29).
    """
    from app.app.services.tz import summary_jobs_queued

    started = time.monotonic()
    waiting = summary_jobs_queued(db, artifact.project_id)
    if waiting:
        log_event(logger, "summary.deferred", project=artifact.project_id, artifact=artifact.artifact_id, waiting=waiting)
        return {"rebuilt": False, "deferred": waiting, "seconds": 0.0}
    rebuilt = rebuild_project_summary(db, settings, artifact.project_id, summary=f"auto after checks {artifact.artifact_id}")
    return {"rebuilt": rebuilt, "seconds": round(time.monotonic() - started, 1)}


def _reextract_left(db: Session, artifact: Artifact) -> int:
    """Сколько ещё переизвлечений этого проекта в очереди (кроме текущего материала)."""
    from sqlalchemy import func

    from app.app.models.job import Job

    return int(db.scalar(
        select(func.count()).select_from(Job).join(Artifact, Artifact.id == Job.artifact_id)
        .where(Artifact.project_id == artifact.project_id, Artifact.id != artifact.id, Job.status.in_(("queued", "running")),
               Job.payload_json.like(f'%"scope": "{SCOPE}"%'))
    ) or 0)


def rebuild_project_summary(db: Session, settings: Settings, project_id: int, *,
                            summary: str = "пересборка после переизвлечения требований") -> bool:
    """Итоговая выжимка проекта собирается из фрагментов текста и пунктов (``rebuild_tz_fast``) — после переизвлечения и
    проверок нового материала её нужно пересобрать, иначе в ней старые пункты (один вызов модели). Отчёты материалов пункты
    не показывают — их не трогаем. Вернуть, собрана ли выжимка."""
    if settings.dry_run:
        return False
    from app.app.services.gpu_phase import prepare_for_llm
    from app.app.services.project_tasks import TASK_TZ_REBUILD, get_running_task, track_project_task
    from app.app.services.tz import rebuild_tz_fast

    if get_running_task(db, task_type=TASK_TZ_REBUILD, project_id=project_id):
        return False
    db.commit()  # соединение БД не держим на время ответа модели (DECISIONS 2026-09-21)
    try:
        prepare_for_llm(settings.lmstudio_model, settings=settings)  # перед задачей очереди мог идти ASR, выгрузивший модель
        with track_project_task(db, task_type=TASK_TZ_REBUILD, project_id=project_id):
            rebuild_tz_fast(db, settings, project_id, summary=summary)
    except Exception:  # noqa: BLE001 — выжимку можно пересобрать кнопкой, требования уже обновлены
        db.rollback()
        logger.exception("project summary rebuild failed for project %s", project_id)
        return False
    return True
