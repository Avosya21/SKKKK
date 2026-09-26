"""Поставить в очередь текстовые артефакты с ошибкой или без извлечённого текста."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from sqlalchemy import or_, select

from app.app.config import get_settings
from app.app.db import SessionLocal
from app.app.models.artifact import Artifact
from app.app.models.job import Job

TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".csv", ".log", ".json", ".yaml", ".yml", ".ini", ".rtf", ".text")


def _is_text_artifact(art: Artifact) -> bool:
    if art.file_type == "txt":
        return True
    name = art.original_filename.lower()
    return any(name.endswith(ext) for ext in TEXT_SUFFIXES)


def requeue_text_artifacts(*, project_id: int | None = None, all_failed: bool = False) -> int:
    settings = get_settings()
    db = SessionLocal()
    try:
        q = select(Artifact)
        if project_id is not None:
            q = q.where(Artifact.project_id == project_id)
        artifacts = db.scalars(q).all()

        count = 0
        for art in artifacts:
            if not _is_text_artifact(art):
                continue
            needs = all_failed or art.status == "error" or (
                art.error_message and "no text extracted" in art.error_message.lower()
            )
            if not needs:
                continue

            path = Path(art.file_path)
            if not path.is_file() or path.stat().st_size == 0:
                print(
                    f"skip empty/missing #{art.id} {art.artifact_id} "
                    f"({art.original_filename})"
                )
                continue

            art.status = "uploaded"
            art.error_message = None
            art.transcription_text = None
            art.transcription_file_path = None
            art.transcript_html_url = None

            db.add(
                Job(
                    artifact_id=art.id,
                    kind="process_artifact",
                    status="queued",
                    phase="llm",
                    attempts=0,
                    error_message=None,
                )
            )
            count += 1
            print(
                f"requeued #{art.id} {art.artifact_id} "
                f"({art.original_filename}) project={art.project_id}"
            )

        if count:
            db.commit()
        else:
            print("nothing to requeue")
        return count
    finally:
        db.close()


if __name__ == "__main__":
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    requeue_text_artifacts(project_id=pid, all_failed="--all" in sys.argv)
