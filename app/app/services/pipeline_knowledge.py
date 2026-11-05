"""Конвейер знаний после получения текста артефакта: сущности, индекс и граф (`run_knowledge_pipeline`)."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.llm_labels import llm_runtime_label
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.services.artifact_reports import document_report_url, write_document_report
from app.app.services.storage import artifact_report_url
from app.app.services.entities import extract_entities_llm, persist_entities, stub_entities
from app.app.services.gpu_phase import prepare_for_llm
from app.app.services.graph import seed_graph
from app.app.services.indexing import index_artifact
from app.app.services.pipeline_audio import run_call_summary
from app.app.services.notify import notify_artifact_user
from app.app.services.summarize_document import run_document_summary
from app.app.services.status import set_status
from app.app.services.tz import maybe_auto_rebuild_tz

logger = logging.getLogger("primaryanalitics.knowledge")


def _enqueue_auto_review(db: Session, settings: Settings, artifact: Artifact) -> None:
    """Фоновая автопроверка требований и конфликтов артефакта (низкий приоритет в очереди). Сбой постановки знания не ломает.

    Последней в цепочке — итоговая выжимка проекта (scope ``summary``): к ней актуальность и конфликты уже выключат
    заменённое. Из партии её собирает только последняя такая задача проекта (``requirements_reextract.summary_for_review``).
    """
    if settings.dry_run:
        return
    from app.app.services.auto_review import MODE_OFF, review_mode
    from app.app.services.queue import enqueue_auto_review

    try:
        mode = review_mode(settings, artifact.project)
        if mode == MODE_OFF:
            return
        # требования → темы → актуальность (дубли, заменённое) → конфликты → выжимка проекта: очередь берёт задачи одного
        # вида по порядку постановки
        for scope in ("artifact", "topics", "relevance", "conflicts", "summary"):
            job = enqueue_auto_review(db, artifact.id, scope=scope, mode=mode)
            if job is not None:
                log_event(logger, "review.queued", artifact=artifact.artifact_id, job_id=job.id, scope=scope, mode=mode)
        from app.app.services.memory_pipeline import enqueue_for_artifact

        for scope in enqueue_for_artifact(db, settings, artifact, mode):  # память решений: акты материала и итог проекта (если включена)
            log_event(logger, "review.queued", artifact=artifact.artifact_id, scope=scope, mode=mode)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("auto review enqueue failed for %s", artifact.artifact_id)


def _extraction_context(db: Session, settings: Settings, artifact: Artifact) -> str:
    """Карточка проекта, оглавление созвона, темы (REQUIREMENTS-PLAN этап 3). Сбой контекста извлечение не ломает."""
    from app.app.services.extraction_context import build_extraction_context

    try:
        return build_extraction_context(db, settings, artifact)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("extraction context failed for %s", artifact.artifact_id)
        return ""


def _suggest_speaker_names(db: Session, settings: Settings, artifact: Artifact) -> None:
    """Подсказки имён неназванных голосов (модель уже загружена для знаний). Сбой подсказки знания не ломает."""
    from app.app.services.speaker_auto import refresh_speaker_hints

    try:
        refresh_speaker_hints(db, settings, artifact)
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("speaker name hints failed for %s", artifact.artifact_id)


def run_knowledge_pipeline(db: Session, settings: Settings, artifact: Artifact, text: str) -> None:
    log_event(
        logger,
        "knowledge.start",
        artifact=artifact.artifact_id,
        file=artifact.original_filename,
        chars=len(text),
        dry_run=settings.dry_run,
        llm_provider=settings.llm_provider,
        llm=llm_runtime_label(settings.llm_provider),
    )

    if settings.dry_run:
        payload = stub_entities(artifact, text)
    else:
        prepare_for_llm(settings.lmstudio_model, settings=settings)
        context = _extraction_context(db, settings, artifact)
        # Закрыть транзакцию: извлечение идёт минутами, соединение из пула всё это время было бы занято.
        db.commit()
        payload = extract_entities_llm(settings, artifact, text, context=context)
    log_event(
        logger,
        "entities.extracted",
        artifact=artifact.artifact_id,
        decisions=len(payload.decisions),
        tasks=len(payload.tasks),
        facts=len(payload.facts),
        mentions=len(payload.mentions),
        participants=len(payload.participants),
    )
    out_path = persist_entities(db, settings, artifact, payload)
    log_event(logger, "entities.saved", artifact=artifact.artifact_id, file=out_path.name)
    set_status(db, artifact, "entities_extracted")

    if artifact.file_type == "audio":
        artifact.transcript_html_url = artifact_report_url(artifact.artifact_id)
        db.commit()
    else:
        report_path = write_document_report(settings, artifact, payload, text)
        artifact.transcript_html_url = document_report_url(artifact)
        db.commit()
        log_event(
            logger,
            "document.report.ready",
            artifact=artifact.artifact_id,
            file=report_path.name,
            url=artifact.transcript_html_url,
        )

    if settings.dry_run:
        log_event(logger, "index.skipped", artifact=artifact.artifact_id, reason="dry_run")
    else:
        chunks = index_artifact(db, settings, artifact, text)
        log_event(
            logger,
            "index.stored",
            artifact=artifact.artifact_id,
            chunks=chunks,
            model=settings.ollama_embed_model,
        )
    set_status(db, artifact, "indexed")

    if artifact.file_type == "audio" and not settings.dry_run:
        run_call_summary(settings, artifact)
        _suggest_speaker_names(db, settings, artifact)
    elif artifact.file_type != "audio" and not settings.dry_run:
        run_document_summary(settings, artifact)
        report_path = write_document_report(settings, artifact, payload, text)
        artifact.transcript_html_url = document_report_url(artifact)
        db.commit()
        log_event(
            logger,
            "document.report.ready",
            artifact=artifact.artifact_id,
            file=report_path.name,
            url=artifact.transcript_html_url,
            after="doc_summary",
        )

    if settings.dry_run:
        log_event(logger, "graph.skipped", artifact=artifact.artifact_id, reason="dry_run")
    else:
        seed_graph(db, settings, artifact, text)
    set_status(db, artifact, "graph_seeded")

    # Выжимка проекта: с автопроверкой — задачей очереди за проверками (одна на партию), сборку здесь
    # maybe_auto_rebuild_tz пропускает; без автопроверки — сразу: документ — в конце партии, созвон — каждый N-й.
    set_status(db, artifact, "done")
    _enqueue_auto_review(db, settings, artifact)
    maybe_auto_rebuild_tz(db, settings, artifact)
    log_event(
        logger,
        "knowledge.done",
        artifact=artifact.artifact_id,
        file=artifact.original_filename,
        project_id=artifact.project_id,
    )

    notify_artifact_user(db, settings, artifact, f"Обработка завершена: {artifact.original_filename}")
