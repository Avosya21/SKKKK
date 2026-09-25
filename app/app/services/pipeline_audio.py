"""Обработка аудио-артефакта: запуск лабораторного ASR-конвейера, стенограмма, сверка спикеров, аудит реплик и выжимка созвона (`process_audio_artifact`)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.llm_labels import llm_runtime_label
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.entity import AsrRun
from app.app.services.artifact_audio import finalize_audio_outputs
from app.app.services.media_cleanup import maybe_discard_video_source, reconcile_artifact_source_path
from app.app.services.pipeline_resume import should_continue_asr
from app.app.services.notify import notify_artifact_user
from app.app.services.status import set_status
from app.app.services.storage import artifact_audio_path, artifact_audio_url, ensure_artifact_workspace

logger = logging.getLogger("skkkk.pipeline")
ROOT = Path(__file__).resolve().parents[3]


def _record_asr_runs(db: Session, artifact: Artifact, asr_dir: Path) -> None:
    summary_path = asr_dir / "pipeline_asr_summary.json"
    if not summary_path.is_file():
        return
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    for engine, row in data.items():
        if not isinstance(row, dict):
            continue
        status = "ok" if row.get("ok") else "error"
        db.add(
            AsrRun(
                artifact_id=artifact.id,
                engine=str(engine),
                status=status,
                duration_sec=row.get("elapsed_sec"),
                error_message=row.get("error"),
            )
        )
        log_event(
            logger,
            "asr.engine",
            artifact=artifact.artifact_id,
            engine=engine,
            status=status,
            seconds=row.get("elapsed_sec"),
            error=row.get("error"),
        )


def _build_transcript_view(env: dict[str, str]) -> None:
    path = ROOT / "asr" / "build_transcript_view.py"
    if path.is_file():
        subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT),
            env=env,
            check=False,
            timeout=600,
        )


def run_call_summary(settings: Settings, artifact: Artifact) -> bool:
    """Выжимка созвона: после спикеров и индекса, LLM + обновление HTML."""
    from app.app.services.artifact_audio import sync_transcript_view_reports

    env = _asr_env(settings, artifact)
    path = ROOT / "asr" / "summarize_call.py"
    if not path.is_file():
        return False
    log_event(
        logger,
        "call_summary.start",
        artifact=artifact.artifact_id,
        llm_provider=settings.llm_provider,
        llm=llm_runtime_label(settings.llm_provider),
    )
    proc = subprocess.run(
        [sys.executable, str(path)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=3600,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        log_event(
            logger,
            "call_summary.failed",
            artifact=artifact.artifact_id,
            llm_provider=settings.llm_provider,
            error=tail[-500:],
        )
        return False
    from app.app.services.artifact_lab import latest_run_id  # noqa: PLC0415

    if latest_run_id(settings, artifact):
        from app.app.services.pipeline_lab import republish_named_transcript  # noqa: PLC0415

        try:
            republish_named_transcript(settings, artifact)
        except FileNotFoundError as exc:
            logger.warning("lab view after summary skipped for %s: %s", artifact.artifact_id, exc)
    sync_transcript_view_reports(settings, artifact)
    log_event(
        logger,
        "call_summary.ok",
        artifact=artifact.artifact_id,
        llm_provider=settings.llm_provider,
    )
    return True


def _asr_env(settings: Settings, artifact: Artifact) -> dict[str, str]:
    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    env = os.environ.copy()
    env["SKKKK_ASR_WORK"] = str(ws / "audio")
    env["SKKKK_ASR_OUT"] = str(ws / "asr")
    env["SKKKK_AUDIO_URL"] = artifact_audio_url(artifact.artifact_id)
    env["SKKKK_ARTIFACT_ID"] = artifact.artifact_id
    env["SKKKK_PROJECT_ID"] = str(artifact.project_id)
    env["SKKKK_MATCH_JSON"] = str(ws / "reports" / "sortformer_speaker_match.json")
    env["LLM_PROVIDER"] = settings.llm_provider
    env["LMSTUDIO_BASE_URL"] = settings.lmstudio_base_url
    env["LMSTUDIO_MODEL"] = settings.lmstudio_model
    env["LMSTUDIO_API_KEY"] = settings.lmstudio_api_key
    if settings.cursor_api_key:
        env["CURSOR_API_KEY"] = settings.cursor_api_key
    env["CURSOR_MODEL"] = settings.cursor_model
    env["CURSOR_RUNTIME"] = settings.cursor_runtime
    if settings.cursor_cloud_repo:
        env["CURSOR_CLOUD_REPO"] = settings.cursor_cloud_repo
    if settings.gigaam_api_key:
        env["GIGAAM_API_KEY"] = settings.gigaam_api_key
    env["WHISPER_URL"] = settings.whisper_url
    env["GIGAAM_URL"] = settings.gigaam_url
    env["TONE_URL"] = settings.tone_url
    env["DIAR_MODEL"] = settings.diar_model
    return env


def _run_speaker_match(env: dict[str, str], artifact: Artifact) -> None:
    """Sortformer segments → WeSpeaker match → JSON в reports/."""
    import sys

    asr_dir = ROOT / "asr"
    if str(asr_dir) not in sys.path:
        sys.path.insert(0, str(asr_dir))
    from match_sortformer_speakers import run  # noqa: PLC0415

    asr_out = Path(env["SKKKK_ASR_OUT"])
    audio = Path(env["SKKKK_ASR_WORK"]) / "input_16k.wav"
    sortformer = asr_out / "sortformer_segments.json"
    if not sortformer.is_file():
        sortformer = Path(env["SKKKK_MATCH_JSON"]).parent / "sortformer_segments.json"
    if not sortformer.is_file() or not audio.is_file():
        log_event(logger, "speaker_match.skip", artifact=artifact.artifact_id, reason="no sortformer or wav")
        return
    out_json = Path(env["SKKKK_MATCH_JSON"])
    out_json.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(
            sortformer_json=sortformer,
            audio=audio,
            project_id=artifact.project_id,
            artifact_id=artifact.artifact_id,
            out_json=out_json,
        )
        log_event(logger, "speaker_match.ok", artifact=artifact.artifact_id, path=str(out_json))
    except SystemExit as exc:
        log_event(logger, "speaker_match.warn", artifact=artifact.artifact_id, error=str(exc))
    except OSError as exc:
        log_event(logger, "speaker_match.warn", artifact=artifact.artifact_id, error=str(exc))


def _run_voice_turn_audit(env: dict[str, str], artifact: Artifact) -> dict:
    """Почти-уверенные чужие метки реплик → auto_reassign до transcript_ready/index."""
    import sys

    asr_dir = ROOT / "asr"
    if str(asr_dir) not in sys.path:
        sys.path.insert(0, str(asr_dir))
    from voice_turn_audit import run as voice_audit_run  # noqa: PLC0415

    asr_out = Path(env["SKKKK_ASR_OUT"])
    speakers = asr_out / "docker_gigaam_speakers.json"
    audio = Path(env["SKKKK_ASR_WORK"]) / "input_16k.wav"
    if not speakers.is_file() or not audio.is_file():
        log_event(
            logger,
            "voice_audit.skip",
            artifact=artifact.artifact_id,
            reason="no speakers json or wav",
        )
        return {"ok": False, "skipped": True, "auto_n": 0}
    try:
        report = voice_audit_run(speakers_json=speakers, audio=audio, out_dir=asr_out, apply=True)
        log_event(
            logger,
            "voice_audit.ok",
            artifact=artifact.artifact_id,
            auto_n=report.get("auto_n", 0),
            skip_n=report.get("skip_n", 0),
            skipped=bool(report.get("skipped")),
        )
        return report
    except SystemExit as exc:
        log_event(logger, "voice_audit.warn", artifact=artifact.artifact_id, error=str(exc))
    except (OSError, RuntimeError, FileNotFoundError) as exc:
        log_event(logger, "voice_audit.warn", artifact=artifact.artifact_id, error=str(exc))
    return {"ok": False, "skipped": True, "auto_n": 0}


def process_audio_artifact(db: Session, settings: Settings, artifact: Artifact) -> str:
    from app.app.services.artifact_lab import latest_run_id

    if latest_run_id(settings, artifact):
        raise RuntimeError(
            f"legacy Whisper/T-one ASR refused for lab artifact {artifact.artifact_id}"
        )

    src = Path(artifact.file_path)
    wav = artifact_audio_path(settings, artifact.artifact_id, artifact.project_id)
    if not src.is_file():
        if wav.is_file():
            src = wav
        else:
            raise FileNotFoundError(f"missing audio {artifact.file_path}")

    env = _asr_env(settings, artifact)
    asr_dir = Path(env["SKKKK_ASR_OUT"])

    for st in ("preprocessing", "asr_running"):
        set_status(db, artifact, st)

    cmd = [sys.executable, str(ROOT / "asr" / "run_pipeline.py"), "--source", str(src)]
    if should_continue_asr(settings, artifact):
        cmd.append("--continue")
        log_event(logger, "asr.continue", artifact=artifact.artifact_id)
    log_event(
        logger,
        "asr.start",
        artifact=artifact.artifact_id,
        file=src.name,
        engines="whisper+tone+gigaam",
        llm_provider=settings.llm_provider,
        llm=llm_runtime_label(settings.llm_provider),
    )
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=7200)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        log_event(logger, "asr.failed", artifact=artifact.artifact_id, exit_code=proc.returncode, error=tail[-300:])
        raise RuntimeError(f"ASR pipeline failed: {tail}")

    if maybe_discard_video_source(src, wav, artifact_id=artifact.artifact_id):
        artifact.file_path = str(wav)
        db.commit()

    new_path = reconcile_artifact_source_path(artifact.file_path, wav)
    if new_path:
        artifact.file_path = new_path
        db.commit()

    log_event(
        logger,
        "asr.finished",
        artifact=artifact.artifact_id,
        seconds=round(time.monotonic() - started, 1),
    )
    set_status(db, artifact, "asr_completed")
    _record_asr_runs(db, artifact, asr_dir)
    db.commit()

    _build_transcript_view(env)
    _run_speaker_match(env, artifact)
    _build_transcript_view(env)
    audit_report = _run_voice_turn_audit(env, artifact)
    if audit_report.get("auto_n"):
        log_event(
            logger,
            "voice_audit.applied",
            artifact=artifact.artifact_id,
            auto_n=audit_report.get("auto_n"),
        )
        _build_transcript_view(env)
    try:
        final_path, transcript_url = finalize_audio_outputs(settings, artifact)
    except FileNotFoundError:
        whisper = asr_dir / "docker_whisper.txt"
        tone = asr_dir / "docker_tone.txt"
        giga = asr_dir / "docker_gigaam.txt"
        empty = all(not p.is_file() or not p.read_text(encoding="utf-8").strip() for p in (whisper, tone, giga))
        if empty:
            artifact.error_message = "речь не обнаружена"
            set_status(db, artifact, "done", reason="речь не обнаружена")
            return ""
        artifact.error_message = "ASR finished but no merged transcript found"
        set_status(db, artifact, "error", reason="no merged transcript")
        raise RuntimeError(artifact.error_message)

    text = final_path.read_text(encoding="utf-8")
    artifact.transcription_file_path = str(final_path)
    artifact.transcription_text = text
    artifact.transcript_html_url = transcript_url
    asr_summary = asr_dir / "pipeline_asr_summary.json"
    if asr_summary.is_file():
        artifact.asr_engines_json = asr_summary.read_text(encoding="utf-8")
    log_event(logger, "transcript.ready", artifact=artifact.artifact_id, chars=len(text), file=final_path.name)
    set_status(db, artifact, "transcript_ready")

    notify_artifact_user(db, settings, artifact, f"Стенограмма готова: {artifact.original_filename}")
    return text
