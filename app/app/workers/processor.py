"""Заглушка обработки для режима DRY_RUN: проходит статусы без моделей."""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.services.entities import persist_entities, stub_entities
from app.app.services.status import set_status
from app.app.services.storage import artifact_root, parsed_dir, structured_dir

logger = logging.getLogger("skkkk.processor")

AUDIO_STATUSES = [
    "preprocessing",
    "asr_running",
    "asr_completed",
    "transcript_ready",
    "entities_extracted",
    "indexed",
    "graph_seeded",
    "done",
]

DOC_STATUSES = [
    "preprocessing",
    "text_extracted",
    "entities_extracted",
    "indexed",
    "graph_seeded",
    "done",
]


def _write_stub(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def process_dry_run(db: Session, settings: Settings, artifact: Artifact) -> None:
    tmp = artifact_root(settings, artifact.project_id, artifact.artifact_id)
    tmp.mkdir(parents=True, exist_ok=True)
    log_event(
        logger,
        "pipeline.start",
        artifact=artifact.artifact_id,
        file=artifact.original_filename,
        file_type=artifact.file_type,
        mode="dry_run",
    )
    try:
        chain = AUDIO_STATUSES if artifact.file_type == "audio" else DOC_STATUSES
        out_dir = parsed_dir(settings, artifact.project_id, artifact.source_type)
        base = artifact.artifact_id
        text = ""
        if artifact.file_type == "audio":
            for st in chain[:4]:
                set_status(db, artifact, st)
                time.sleep(0.005)
            _write_stub(out_dir / f"{base}_raw_whisper.txt", "whisper stub\n")
            _write_stub(out_dir / f"{base}_raw_tone.txt", "tone stub\n")
            _write_stub(out_dir / f"{base}_raw_gigaam.txt", "gigaam stub\n")
            final = out_dir / f"{base}_final.txt"
            text = "Спикер №1: dry run transcript\n"
            _write_stub(final, text)
            artifact.transcription_file_path = str(final)
            artifact.transcription_text = text
            set_status(db, artifact, "transcript_ready")
        else:
            for st in chain[:2]:
                set_status(db, artifact, st)
                time.sleep(0.005)
            clean = out_dir / f"{base}_clean.txt"
            text = f"DRY RUN content for {artifact.original_filename}\n"
            _write_stub(out_dir / f"{base}_raw.txt", text)
            _write_stub(clean, text)
            artifact.transcription_file_path = str(clean)
            artifact.transcription_text = text
            set_status(db, artifact, "text_extracted")

        for st in chain[4 if artifact.file_type == "audio" else 2 :]:
            if st == "done":
                continue
            set_status(db, artifact, st)
            time.sleep(0.005)

        payload = stub_entities(artifact, text)
        out_path = persist_entities(db, settings, artifact, payload)
        log_event(logger, "entities.saved", artifact=artifact.artifact_id, file=out_path.name)
        structured_dir(settings, artifact.project_id).mkdir(parents=True, exist_ok=True)
        set_status(db, artifact, "done")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
