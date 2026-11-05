"""GPU-воркер: один процесс, берёт задачи из очереди по одной, ждёт зависимости и запускает обработку артефактов."""

from __future__ import annotations

import logging
import os
import time

try:
    from loguru import logger as log
except ImportError:  # loguru missing in this interpreter -> colored stdlib fallback
    import sys as _sys

    try:
        from colorama import Fore as _Fore, Style as _Style
    except ImportError:
        _Fore, _Style = None, None

    _C = {
        logging.INFO: getattr(_Fore, "CYAN", ""),
        logging.WARNING: getattr(_Fore, "YELLOW", ""),
        logging.ERROR: getattr(_Fore, "RED", ""),
    }
    _R = getattr(_Style, "RESET", "")

    class _Shim:
        def __init__(self):
            _h = logging.StreamHandler(_sys.stderr)
            _h.setFormatter(logging.Formatter(
                fmt="%(asctime)s | %(levelname)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            self._lg = logging.getLogger("primaryanalitics.gpu_worker")
            self._lg.setLevel(logging.INFO)
            self._lg.propagate = False
            if not self._lg.handlers:
                self._lg.addHandler(_h)

        @staticmethod
        def _fmt(msg, args, kwargs):
            try:
                return msg.format(*args, **kwargs)
            except Exception:
                return " ".join([str(msg), *[str(x) for x in args], *[f"{k}={v}" for k, v in kwargs.items()]])

        def _emit(self, level, msg, args, kwargs, exc=False):
            self._lg.log(level, _C.get(level, "") + self._fmt(msg, args, kwargs) + _R, exc_info=exc)

        def info(self, msg, *a, **k):
            self._emit(logging.INFO, msg, a, k)

        def warning(self, msg, *a, **k):
            self._emit(logging.WARNING, msg, a, k)

        def error(self, msg, *a, **k):
            self._emit(logging.ERROR, msg, a, k)

        def exception(self, msg, *a, **k):
            self._emit(logging.ERROR, msg, a, k, exc=True)

    log = _Shim()

from app.app.config import get_settings
from app.app.db import SessionLocal
from app.app.logging_setup import log_event, setup_logging
from app.app.models.artifact import Artifact
from app.app.services.queue import (
    KIND_AUTO_REVIEW,
    KIND_PROCESS,
    KIND_PROJECT_TASK,
    KIND_REFRESH,
    WORKER_KINDS,
    claim_next_job,
    finish_claimed_job,
    finish_job,
    requeue_orphaned_jobs,
)
from app.app.services.single_instance import AlreadyRunning, SingleInstance
from app.app.workers import processor as proc_mod

logger = logging.getLogger("primaryanalitics.worker")


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


def _run_other_job(db, settings, job, artifact: Artifact) -> bool:
    """Задачи кроме расшифровки. Неизвестный вид — ошибка задачи, а не запуск конвейера расшифровки на готовом файле.

    Ошибка автопроверки статус артефакта не трогает: знания по нему уже построены.
    """
    started = time.monotonic()
    job_pk = job.id
    job_kind = job.kind
    # Артефакт могут удалить посреди задачи: после rollback обращение к его полям само упадёт.
    artifact_id = artifact.artifact_id
    artifact_pk = artifact.id
    try:
        if job.kind not in (KIND_AUTO_REVIEW, KIND_REFRESH, KIND_PROJECT_TASK):
            raise ValueError(f"неизвестный вид задачи: {job.kind!r}")
        if job.kind == KIND_PROJECT_TASK:
            # кнопки портала сами понимают DRY_RUN (заглушки вместо модели), как раньше в процессе API
            from app.app.workers.project_jobs import run_project_task

            run_project_task(db, settings, job, artifact)
        elif settings.dry_run:
            log_event(logger, "job.skipped", artifact=artifact.artifact_id, kind=job.kind, reason="dry_run")
        elif job.kind == KIND_REFRESH:
            from app.app.workers.review import refresh_knowledge

            refresh_knowledge(db, settings, artifact)
        else:
            from app.app.workers.review import process_review

            process_review(db, settings, job, artifact)
        finish_claimed_job(db, job_pk, artifact_pk, ok=True)
        log_event(logger, "job.done", job_id=job_pk, artifact=artifact_id, kind=job_kind, seconds=round(time.monotonic() - started, 1))
    except Exception as exc:  # noqa: BLE001
        logger.exception("job %s failed", job_pk)
        db.rollback()
        finish_claimed_job(db, job_pk, artifact_pk, ok=False, error=str(exc)[:1000])
        log_event(logger, "job.failed", job_id=job_pk, artifact=artifact_id, kind=job_kind, error=exc)
    return True


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
            kind=job.kind,
        )
        if job.kind != KIND_PROCESS:
            return _run_other_job(db, settings, job, artifact)
        # Встречу могут удалить посреди расшифровки: дальше опираемся на запомненные ключи, а не на объекты сессии.
        job_pk, artifact_pk, artifact_key = job.id, artifact.id, artifact.artifact_id
        started = time.monotonic()
        try:
            if settings.dry_run:
                proc_mod.process_dry_run(db, settings, artifact)
            else:
                from app.app.workers.pipeline import process_artifact

                log.info(
                    "[HEAVY] start | Parakeet-TDT -> GigaAM -> Sortformer -> LightRAG | "
                    "artifact={artifact} file={file} attempt={attempt}",
                    artifact=artifact.artifact_id,
                    file=artifact.original_filename,
                    attempt=job.attempts,
                )
                phase_started = time.monotonic()
                try:
                    process_artifact(db, settings, artifact)
                except Exception as e:  # noqa: BLE001
                    log.exception(
                        "[HEAVY] FAILED | Parakeet-TDT / GigaAM / Sortformer / LightRAG | "
                        "artifact={artifact} error={err}",
                        artifact=artifact.artifact_id,
                        err=str(e),
                    )
                    raise
                log.info(
                    "[HEAVY] done | Parakeet-TDT / GigaAM / Sortformer / LightRAG | "
                    "artifact={artifact} seconds={seconds}",
                    artifact=artifact.artifact_id,
                    seconds=round(time.monotonic() - phase_started, 1),
                )
            if not finish_claimed_job(db, job_pk, artifact_pk, ok=True):
                log_event(logger, "job.gone", job_id=job_pk, artifact=artifact_key, reason="artifact deleted during job")
                return True
            log_event(
                logger,
                "job.done",
                job_id=job_pk,
                artifact=artifact_key,
                status=artifact.status,
                seconds=round(time.monotonic() - started, 1),
            )
        except Exception as exc:
            logger.exception("job %s failed", job_pk)
            from pathlib import Path

            from app.app.services.pipeline_resume import try_resume_transcript

            db.rollback()
            artifact = db.get(Artifact, artifact_pk)
            if artifact is None or artifact.artifact_id != artifact_key:
                # Встречу удалили: номер задачи мог уже достаться чужой — не трогаем.
                finish_claimed_job(db, job_pk, artifact_pk, ok=False, error="artifact deleted during job")
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
            from app.app.services.lab_errors import sanitize_lab_error

            artifact.error_message = sanitize_lab_error(str(exc))
            db.commit()
            finish_claimed_job(db, job_pk, artifact_pk, ok=False, error=sanitize_lab_error(str(exc)))
            log_event(
                logger,
                "job.failed",
                job_id=job_pk,
                artifact=artifact_key,
                seconds=round(time.monotonic() - started, 1),
                error=exc,
            )
            if artifact.file_type != "audio":
                # Последний файл партии упал — ТЗ по остальным всё равно нужно пересобрать.
                try:
                    from app.app.services.tz import maybe_auto_rebuild_tz

                    maybe_auto_rebuild_tz(db, settings, artifact)
                except Exception:
                    logger.exception("auto rebuild after failed job %s", job_pk)
        return True
    finally:
        db.close()


def _unload_llm_if_idle(settings) -> None:
    """Очередь пуста: выгрузить модель после простоя LMSTUDIO_IDLE_UNLOAD_SEC, если и чат её не ждёт (llm_keeper)."""
    try:
        from app.app.services.llm_keeper import maybe_unload_idle

        maybe_unload_idle(settings)
    except Exception:  # noqa: BLE001 — сбой выгрузки не должен останавливать очередь
        logger.exception("llm idle unload failed")


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
        from app.app.services.llm import announce_llm_and_selfcheck

        announce_llm_and_selfcheck(settings, logger)
        log_event(
            logger,
            "worker.started",
            pid=os.getpid(),
            dry_run=settings.dry_run,
            data_dir=settings.data_dir,
            # по этой метке API и скрипты проверяют, что работающий воркер понимает новые виды задач
            kinds=",".join(WORKER_KINDS),
        )
        while True:
            try:
                busy = run_once()
            except Exception:  # noqa: BLE001
                # Сбой одной задачи не должен останавливать очередь: API воркер не перезапускает.
                logger.exception("run_once crashed; worker continues")
                busy = False
            if not busy:
                _unload_llm_if_idle(settings)
                time.sleep(2)
    finally:
        guard.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
