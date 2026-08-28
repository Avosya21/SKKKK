from __future__ import annotations

import logging
import os
import time

from app.app.config import get_settings
from app.app.db import SessionLocal
from app.app.logging_setup import log_event, setup_logging
from app.app.models.artifact import Artifact
from app.app.services.queue import claim_next_job, finish_job, requeue_orphaned_jobs
from app.app.services.single_instance import AlreadyRunning, SingleInstance
from app.app.workers import processor as proc_mod

logger = logging.getLogger("skkkk.worker")


def _wait_dependencies(settings, timeout_sec: int = 300) -> None:
    import httpx

    from app.app.llm_settings import uses_local_gemma
    from app.app.services.ollama_runtime import ensure_ollama

    need_lm = uses_local_gemma(settings.llm_provider)
    try:
        ensure_ollama(settings, timeout=min(timeout_sec, 120))
    except RuntimeError as exc:
        logger.warning("Ollama auto-start failed: %s", exc)

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        ok_lm = True if not need_lm else False
        ok_ollama = False
        if need_lm:
            try:
                with httpx.Client(timeout=5) as client:
                    client.get(f"{settings.lmstudio_base_url.rstrip('/')}/models")
                ok_lm = True
            except Exception:
                ok_lm = False
        try:
            with httpx.Client(timeout=5) as client:
                client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
            ok_ollama = True
        except Exception:
            ok_ollama = False
        if ok_lm and ok_ollama:
            if need_lm:
                logger.info("LM Studio and Ollama are reachable")
            else:
                logger.info("Ollama is reachable (LLM_PROVIDER=cursor)")
            return
        if need_lm:
            logger.info("waiting for LM Studio/Ollama...")
        else:
            logger.info("waiting for Ollama...")
        time.sleep(5)
    logger.warning("dependency wait timed out; worker continues anyway")


def run_once() -> bool:
    settings = get_settings()
    db = SessionLocal()
    try:
        job = claim_next_job(db, settings.job_max_attempts)
        if not job:
            return False
        artifact = db.get(Artifact, job.artifact_id)
        if not artifact:
            log_event(logger, "job.aborted", job_id=job.id, reason="artifact missing")
            finish_job(db, job, ok=False, error="artifact missing")
            return True
        log_event(
            logger,
            "job.claimed",
            job_id=job.id,
            artifact=artifact.artifact_id,
            file=artifact.original_filename,
            file_type=artifact.file_type,
            attempt=job.attempts,
        )
        started = time.monotonic()
        try:
            if settings.dry_run:
                proc_mod.process_dry_run(db, settings, artifact)
            else:
                from app.app.workers.pipeline import process_artifact

                process_artifact(db, settings, artifact)
            finish_job(db, job, ok=True)
            log_event(
                logger,
                "job.done",
                job_id=job.id,
                artifact=artifact.artifact_id,
                status=artifact.status,
                seconds=round(time.monotonic() - started, 1),
            )
        except Exception as exc:
            logger.exception("job %s failed", job.id)
            from pathlib import Path

            from app.app.models.job import Job
            from app.app.services.pipeline_resume import try_resume_transcript

            job_pk = job.id
            artifact_pk = job.artifact_id
            db.rollback()
            artifact = db.get(Artifact, artifact_pk)
            job = db.get(Job, job_pk)
            if artifact is None or job is None:
                return True
            transcript_path = artifact.transcription_file_path
            has_transcript = bool(
                transcript_path
                and Path(transcript_path).is_file()
                and Path(transcript_path).stat().st_size > 50
            ) or try_resume_transcript(settings, artifact) is not None
            past_gate = artifact.status in {
                "speakers_confirmed",
                "transcript_ready",
                "text_extracted",
                "entities_extracted",
                "indexed",
                "graph_seeded",
                "done",
            }
            if has_transcript and artifact.file_type == "audio":
                if settings.voice_gate_before_asr and not past_gate:
                    artifact.status = "speakers_pending"
                else:
                    artifact.status = "transcript_ready"
            else:
                artifact.status = "error"
            artifact.error_message = str(exc)
            db.commit()
            finish_job(db, job, ok=False, error=str(exc))
            log_event(
                logger,
                "job.failed",
                job_id=job.id,
                artifact=artifact.artifact_id,
                seconds=round(time.monotonic() - started, 1),
                error=exc,
            )
        return True
    finally:
        db.close()


def main() -> int:
    settings = get_settings()
    setup_logging("worker", settings.data_dir / "logs")

    try:
        guard = SingleInstance("gpu_worker", settings.data_dir / "locks").__enter__()
    except AlreadyRunning as exc:
        logger.error("воркер не запущен: %s", exc)
        return 1

    try:
        _wait_dependencies(settings)
        db = SessionLocal()
        try:
            n = requeue_orphaned_jobs(db, settings.job_max_attempts)
            if n:
                log_event(logger, "jobs.requeued", count=n, reason="worker restart")
        finally:
            db.close()
        log_event(
            logger,
            "worker.started",
            pid=os.getpid(),
            dry_run=settings.dry_run,
            data_dir=settings.data_dir,
        )
        while True:
            if not run_once():
                time.sleep(2)
    finally:
        guard.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
