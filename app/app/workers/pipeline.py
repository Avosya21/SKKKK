from __future__ import annotations

import logging
import shutil

from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.services.pipeline_audio import process_audio_artifact
from app.app.services.pipeline_documents import process_document_artifact
from app.app.services.pipeline_knowledge import run_knowledge_pipeline
from app.app.services.pipeline_resume import try_resume_transcript
from app.app.services.status import set_status
from app.app.services.storage import artifact_root

logger = logging.getLogger("skkkk.pipeline")


def process_artifact(db: Session, settings: Settings, artifact: Artifact) -> None:
    tmp = artifact_root(settings, artifact.project_id, artifact.artifact_id)
    tmp.mkdir(parents=True, exist_ok=True)
    log_event(
        logger,
        "pipeline.start",
        artifact=artifact.artifact_id,
        file=artifact.original_filename,
        file_type=artifact.file_type,
        source_type=artifact.source_type,
    )
    try:
        if artifact.file_type == "audio":
            text = try_resume_transcript(settings, artifact)
            if text is None:
                text = process_audio_artifact(db, settings, artifact)
            elif artifact.status not in {"transcript_ready", "text_extracted"}:
                artifact.transcription_text = text
                if not artifact.transcription_file_path:
                    from app.app.services.storage import parsed_dir

                    final = parsed_dir(settings, artifact.project_id, artifact.source_type) / (
                        f"{artifact.artifact_id}_final.txt"
                    )
                    if final.is_file():
                        artifact.transcription_file_path = str(final)
                set_status(db, artifact, "transcript_ready")
            if not text and artifact.status == "done":
                log_event(logger, "pipeline.stop", artifact=artifact.artifact_id, reason="no speech detected")
                return
        else:
            text = process_document_artifact(db, settings, artifact)

        if artifact.status in {"transcript_ready", "text_extracted"}:
            run_knowledge_pipeline(db, settings, artifact, text)
        elif artifact.status != "done":
            set_status(db, artifact, "done")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
