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
from app.app.services.notify import send_telegram
from app.app.services.status import set_status
from app.app.services.tz import maybe_auto_rebuild_tz

logger = logging.getLogger("skkkk.knowledge")


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
        payload = extract_entities_llm(settings, artifact, text)
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

    if settings.dry_run:
        log_event(logger, "graph.skipped", artifact=artifact.artifact_id, reason="dry_run")
    else:
        seed_graph(db, settings, artifact, text)
    set_status(db, artifact, "graph_seeded")

    # Документы теперь всегда инициируют пересборку ТЗ после полного завершения
    # обработки; для аудио оставляем прежнюю пакетную логику по счётчику done.
    set_status(db, artifact, "done")
    maybe_auto_rebuild_tz(db, settings, artifact)
    log_event(
        logger,
        "knowledge.done",
        artifact=artifact.artifact_id,
        file=artifact.original_filename,
        project_id=artifact.project_id,
    )

    send_telegram(settings, str(artifact.user_id), f"Обработка завершена: {artifact.original_filename}")
