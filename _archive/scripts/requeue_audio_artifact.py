"""Поставить в очередь аудио-артефакт с ошибкой (повтор ASR с начала)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.app.config import get_settings
from app.app.db import SessionLocal
from app.app.models.artifact import Artifact
from app.app.models.job import Job
from app.app.services.storage import artifact_audio_path


def requeue_audio_artifact(artifact_id: str) -> int:
    settings = get_settings()
    db = SessionLocal()
    try:
        art = db.scalar(select(Artifact).where(Artifact.artifact_id == artifact_id))
        if not art:
            raise SystemExit(f"artifact not found: {artifact_id}")
        if art.file_type != "audio":
            raise SystemExit(f"not an audio artifact: {artifact_id} ({art.file_type})")

        src = Path(art.file_path)
        wav = artifact_audio_path(settings, art.artifact_id, art.project_id)
        webm = wav.parent / "input.webm"
        if not src.is_file():
            if webm.is_file():
                art.file_path = str(webm)
                src = webm
            elif wav.is_file():
                art.file_path = str(wav)
                src = wav
            else:
                raise SystemExit(f"source file missing: {art.file_path}")

        art.status = "uploaded"
        art.error_message = None
        art.transcription_text = None
        art.transcription_file_path = None
        art.transcript_html_url = None

        job = Job(
            artifact_id=art.id,
            kind="process_artifact",
            status="queued",
            phase="asr",
            attempts=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        print(
            f"requeued job {job.id} for {art.artifact_id} "
            f"({art.original_filename}) project={art.project_id}"
        )
        return job.id
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/requeue_audio_artifact.py <artifact_id>")
    requeue_audio_artifact(sys.argv[1])
