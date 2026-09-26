"""Resume knowledge pipeline for an artifact with finished ASR/transcript."""
from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import select

from app.app.config import get_settings
from app.app.db import SessionLocal
from app.app.models.artifact import Artifact
from app.app.services.pipeline_knowledge import run_knowledge_pipeline
from app.app.services.pipeline_resume import try_resume_transcript
from app.app.services.queue import enqueue_knowledge_retry
from app.app.services.status import set_status


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume knowledge phase for artifact")
    parser.add_argument("artifact_id", help="e.g. art_2026-08-21_094718_29258f")
    parser.add_argument(
        "--enqueue",
        action="store_true",
        help="Only reset status and queue GPU worker job",
    )
    args = parser.parse_args()

    settings = get_settings()
    db = SessionLocal()
    try:
        artifact = db.scalar(select(Artifact).where(Artifact.artifact_id == args.artifact_id))
        if not artifact:
            raise SystemExit(f"artifact not found: {args.artifact_id}")

        text = try_resume_transcript(settings, artifact)
        if not text:
            path = artifact.transcription_file_path
            if path and Path(path).is_file():
                text = Path(path).read_text(encoding="utf-8")
        if not text.strip():
            raise SystemExit("no transcript text to resume")

        artifact.transcription_text = text
        set_status(db, artifact, "transcript_ready")
        artifact.error_message = None
        db.commit()

        if args.enqueue:
            job = enqueue_knowledge_retry(db, artifact.id)
            print(f"queued job {job.id} for {artifact.artifact_id} ({len(text)} chars)")
            return 0

        run_knowledge_pipeline(db, settings, artifact, text)
        db.commit()
        print(f"done: {artifact.artifact_id} status={artifact.status}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
