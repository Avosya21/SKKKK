"""Voice gate before triple ASR: wav → Sortformer → speaker match → speakers_pending."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.services.artifact_audio import ensure_audio_wav
from app.app.services.media_cleanup import maybe_discard_video_source, reconcile_artifact_source_path
from app.app.services.notify import send_telegram
from app.app.services.pipeline_audio import _asr_env, _run_speaker_match
from app.app.services.speaker_profiles import rebuild_match_report_html
from app.app.services.status import set_status
from app.app.services.storage import artifact_audio_path, ensure_artifact_workspace

logger = logging.getLogger("skkkk.pipeline")

ROOT = Path(__file__).resolve().parents[3]
PYTHON = sys.executable

VOICE_GATE_STEPS = (
    ("audio_prep", "Подготовка WAV"),
    ("sortformer", "Sortformer diarization"),
    ("speakers_match", "Match Sortformer → профили"),
    ("speakers_confirm", "Подтверждение спикеров"),
)


def _checklist_path(ws: Path) -> Path:
    return ws / "asr" / "voice_gate_checklist.json"


def _read_checklist(ws: Path) -> dict:
    path = _checklist_path(ws)
    if not path.is_file():
        return {"steps": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"steps": {}}


def _write_checklist(ws: Path, payload: dict) -> None:
    path = _checklist_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _init_checklist(ws: Path) -> None:
    payload = _read_checklist(ws)
    payload["phase"] = "speakers"
    steps = payload.setdefault("steps", {})
    for step_id, _label in VOICE_GATE_STEPS:
        steps.setdefault(step_id, {"status": "pending"})
    _write_checklist(ws, payload)


def _mark_step(ws: Path, step_id: str, status: str, *, detail: str | None = None) -> None:
    payload = _read_checklist(ws)
    row = payload.setdefault("steps", {}).setdefault(step_id, {})
    row["status"] = status
    if detail:
        row["detail"] = detail
    _write_checklist(ws, payload)


def _mark_asr_checklist_ok(ws: Path, step_id: str, detail: str) -> None:
    """Mark run_pipeline steps so --continue skips wav/sortformer after gate."""
    path = ws / "asr" / "pipeline_checklist.json"
    payload: dict
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {"steps": {}}
    else:
        payload = {"steps": {}}
    payload.setdefault("steps", {}).setdefault(step_id, {})["status"] = "ok"
    payload["steps"][step_id]["detail"] = detail
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_script(rel: str, env: dict[str, str], *, timeout: int = 7200, artifact_id: str, step: str) -> None:
    cmd = [PYTHON, str(ROOT / rel)]
    log_event(logger, "voice_gate.step.start", artifact=artifact_id, step=step)
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=timeout)
    elapsed = round(time.monotonic() - started, 1)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        log_event(logger, "voice_gate.step.failed", artifact=artifact_id, step=step, error=tail[-400:])
        raise RuntimeError(f"{step} failed: {tail}")
    log_event(logger, "voice_gate.step.ok", artifact=artifact_id, step=step, seconds=elapsed)


def _resolve_audio_source(settings: Settings, artifact: Artifact) -> Path:
    src = Path(artifact.file_path)
    wav = artifact_audio_path(settings, artifact.artifact_id, artifact.project_id)
    if not src.is_file():
        if wav.is_file():
            return wav
        raise FileNotFoundError(f"missing audio {artifact.file_path}")
    return src


def _prepare_wav(settings: Settings, artifact: Artifact, src: Path) -> Path:
    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    wav = ws / "audio" / "input_16k.wav"
    if not wav.is_file():
        ensured = ensure_audio_wav(settings, artifact)
        if ensured is None:
            raise FileNotFoundError(f"cannot prepare wav for {artifact.artifact_id}")
        wav = ensured

    if maybe_discard_video_source(src, wav, artifact_id=artifact.artifact_id):
        artifact.file_path = str(wav)
    new_path = reconcile_artifact_source_path(artifact.file_path, wav)
    if new_path:
        artifact.file_path = new_path
    return wav


def run_voice_gate(db: Session, settings: Settings, artifact: Artifact) -> None:
    """Sortformer + match; pause at speakers_pending until user confirms."""
    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    _init_checklist(ws)
    env = _asr_env(settings, artifact)
    src = _resolve_audio_source(settings, artifact)

    set_status(db, artifact, "preprocessing")
    _mark_step(ws, "audio_prep", "running")
    db.commit()
    _prepare_wav(settings, artifact, src)
    _mark_step(ws, "audio_prep", "ok")
    _mark_asr_checklist_ok(ws, "audio_prep", "voice gate")
    db.commit()

    _mark_step(ws, "sortformer", "running")
    _run_script("asr/dump_sortformer.py", env, artifact_id=artifact.artifact_id, step="sortformer")
    _mark_step(ws, "sortformer", "ok")
    _mark_asr_checklist_ok(ws, "sortformer", "voice gate")
    db.commit()

    _mark_step(ws, "speakers_match", "running")
    _run_speaker_match(env, artifact)
    _mark_step(ws, "speakers_match", "ok")
    rebuild_match_report_html(db, project_id=artifact.project_id, artifact_id=artifact.artifact_id)

    _mark_step(ws, "speakers_confirm", "running", detail="ожидание пользователя")
    set_status(db, artifact, "speakers_pending")
    db.commit()
    send_telegram(
        settings,
        str(artifact.user_id),
        f"Подтвердите спикеров: {artifact.original_filename}",
    )
    log_event(logger, "voice_gate.speakers_pending", artifact=artifact.artifact_id)
