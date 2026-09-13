"""Native meeting: per-track dual hearing → merge by time → one transcript."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.app.config import ROOT, Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.services.artifact_audio import _extract_wav_atomic
from app.app.services.artifact_lab import latest_run_id
from app.app.services.lab_errors import NO_SPEECH
from app.app.services.meeting_analyze import (
    KNOWN_SPEAKER_FILE,
    MEETING_KIND,
    meeting_manifest_path,
    track_child_id,
)
from app.app.services.meeting_merge import (
    apply_leaks,
    attach_turn_ids,
    find_leaks,
    merge_track_replicas,
    merge_words,
    mix_track_wavs,
    shift_words,
    words_from_replicas,
)
from app.app.services.meeting_recordings import SOURCE_SCREEN, read_events, speaker_group_key
from app.app.services.notify import notify_artifact_user
from app.app.services.pipeline_lab import (
    APP_PYTHON,
    _lab_env,
    _run_cmd,
    init_lab_checklist,
    lab_python,
    mark_lab_step,
    publish_lab_to_prod,
)
from app.app.services.status import set_status
from app.app.services.storage import artifact_audio_path, artifact_dir, ensure_artifact_workspace

logger = logging.getLogger("skkkk.pipeline")

MEETING_LAB_STEPS: tuple[tuple[str, str], ...] = (
    ("audio_prep", "Сбор дорожек"),
    ("track_asr", "Расшифровка дорожек"),
    ("track_giga", "Второй слух по дорожкам"),
    ("track_proof", "Вычитка дорожек"),
    ("merge", "Сборка одной стенограммы"),
)

TRACK_OVERLAYS: tuple[tuple[str, str, int], ...] = (
    ("lab_giga", "asr/lab_giga.py", 7200),
    ("lab_reconcile", "asr/lab_reconcile.py", 600),
    ("lab_proof", "asr/lab_proof.py", 3600),
)

WORDS_REL = "02_asr/words.json"


def is_meeting_artifact(artifact: Artifact) -> bool:
    raw = getattr(artifact, "asr_engines_json", None) or ""
    if MEETING_KIND not in raw:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return data.get("kind") == MEETING_KIND


def process_meeting_artifact(db: Session, settings: Settings, artifact: Artifact) -> str:
    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    init_lab_checklist(ws, steps=MEETING_LAB_STEPS)
    manifest = _load_manifest(settings, artifact)
    tracks = list(manifest.get("tracks") or [])
    if not tracks:
        raise RuntimeError("нет дорожек встречи")

    set_status(db, artifact, "preprocessing")
    mark_lab_step(ws, "audio_prep", "running")
    db.commit()
    prepared = [_prepare_track(settings, artifact, row) for row in tracks]
    mark_lab_step(ws, "audio_prep", "ok", detail=f"{len(prepared)} дорожек")
    set_status(db, artifact, "asr_running")
    db.commit()

    from app.app.llm_settings import uses_local_gemma

    if uses_local_gemma(settings.llm_provider):
        from app.app.services.gpu_phase import prepare_for_asr

        prepare_for_asr()

    mark_lab_step(ws, "track_asr", "running")
    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    mix_inputs: list[tuple[Path, float]] = []
    spoken = 0
    work = [
        row
        for row in prepared
        if not row.get("silent") and row.get("source") != SOURCE_SCREEN
    ]
    for index, row in enumerate(work, start=1):
        mark_lab_step(
            ws,
            "track_asr",
            "running",
            detail=f"{row['speaker']} ({index}/{len(work)})",
        )
        db.commit()
        words, wav16, had_words = _transcribe_track(settings, artifact, row)
        mix_inputs.append((wav16, float(row.get("offset_sec") or 0.0)))
        key = speaker_group_key(str(row.get("identity") or ""))
        if key not in buckets:
            order.append(key)
            buckets[key] = []
        buckets[key].extend(words)
        if had_words:
            spoken += 1
    groups = [merge_words(buckets[key]) for key in order]
    mark_lab_step(ws, "track_asr", "ok", detail=f"с речью {spoken}/{len(prepared)}")
    mark_lab_step(ws, "track_giga", "ok", detail="по дорожкам с речью")
    mark_lab_step(ws, "track_proof", "ok", detail="по дорожкам с речью")

    mark_lab_step(ws, "merge", "running")
    db.commit()
    events = _load_events(settings, manifest)
    from app.app.services.meeting_overlap import refine_leaks

    leaks = refine_leaks(
        groups,
        find_leaks(groups, events),
        events=events,
        rms=_track_rms_fn(work),
    )
    groups = apply_leaks(groups, leaks)
    tape = merge_words(*groups) if groups else []
    merged = attach_turn_ids(
        merge_track_replicas(*groups) if groups else [],
        artifact.artifact_id,
    )
    mixed = artifact_audio_path(settings, artifact.artifact_id, artifact.project_id)
    if mix_inputs:
        mix_track_wavs(mix_inputs, mixed)
    artifact.file_path = str(mixed)
    _write_parent_proof(settings, artifact, merged, words=tape, leaks=leaks)
    text = publish_lab_to_prod(db, settings, artifact, latest_run_id(settings, artifact) or "")
    if not text:
        artifact.error_message = NO_SPEECH
        set_status(db, artifact, "done", reason=NO_SPEECH)
        mark_lab_step(ws, "merge", "ok", detail=NO_SPEECH)
        return ""
    mark_lab_step(ws, "merge", "ok")
    set_status(db, artifact, "transcript_ready")
    notify_artifact_user(db, settings, artifact, f"Стенограмма готова: {artifact.original_filename}")
    log_event(logger, "meeting.merge.ok", artifact=artifact.artifact_id, replicas=len(merged))
    return text


def _load_manifest(settings: Settings, artifact: Artifact) -> dict:
    path = meeting_manifest_path(settings, artifact.project_id, artifact.artifact_id)
    if not path.is_file():
        raise FileNotFoundError(f"нет {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _prepare_track(settings: Settings, artifact: Artifact, row: dict) -> dict:
    child_id = str(row.get("child_id") or track_child_id(artifact.artifact_id, row["identity"]))
    src = Path(row["src"])
    dest = artifact_dir(settings, artifact.project_id, child_id) / "audio" / "input_16k.wav"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        _extract_wav_atomic(src, dest)
    known = {
        "skip_sortformer": True,
        "speaker": row.get("speaker") or row.get("identity"),
        "identity": row.get("identity"),
    }
    (dest.parent.parent / KNOWN_SPEAKER_FILE).write_text(
        json.dumps(known, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    item = dict(row)
    item["child_id"] = child_id
    item["wav16"] = str(dest)
    return item


def _transcribe_track(settings: Settings, parent: Artifact, row: dict) -> tuple[list[dict], Path, bool]:
    child = SimpleNamespace(project_id=parent.project_id, artifact_id=row["child_id"])
    wav = Path(row["wav16"])
    env = _lab_env(settings, child)
    cmd = [
        str(lab_python()),
        str(ROOT / "asr" / "lab_pipeline.py"),
        "--project-id",
        str(parent.project_id),
        "--artifact-id",
        row["child_id"],
        "--no-voice",
        "--no-verify",
    ]
    _run_cmd(cmd, env, timeout=7200, artifact_id=row["child_id"], step="lab_onnx")
    run_id = latest_run_id(settings, child)
    if not run_id:
        raise RuntimeError("lab_pipeline не оставил run_id")
    words = _track_word_count(settings, child, run_id)
    if words == 0:
        return [], wav, False
    from app.app.services.docker_runtime import ensure_gigaam_ready

    ensure_gigaam_ready(settings.gigaam_url)
    for step_id, rel, timeout in TRACK_OVERLAYS:
        overlay = [
            str(APP_PYTHON),
            str(ROOT / rel),
            "--project-id",
            str(parent.project_id),
            "--artifact-id",
            row["child_id"],
            "--run-id",
            run_id,
        ]
        _run_cmd(overlay, env, timeout=timeout, artifact_id=row["child_id"], step=step_id)
    replicas = _load_track_replicas(settings, child, run_id)
    named = shift_words(
        words_from_replicas(replicas),
        float(row.get("offset_sec") or 0.0),
        str(row["speaker"]),
    )
    return named, wav, True


def _track_word_count(settings: Settings, artifact, run_id: str) -> int:
    from app.app.services.artifact_lab import resolve_run

    path = resolve_run(settings, artifact, run_id).resolve(WORDS_REL)
    if not path.is_file():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return int(payload.get("word_count") or len(payload.get("words") or []))


def _load_track_replicas(settings: Settings, artifact, run_id: str) -> list[dict]:
    from app.app.services.pipeline_lab import _load_proof_replicas

    try:
        return _load_proof_replicas(settings, artifact, run_id)
    except FileNotFoundError:
        return []


def _track_rms_fn(rows: list[dict]):
    wavs = {
        str(row.get("speaker") or row.get("identity") or ""): (
            Path(row["wav16"]),
            float(row.get("offset_sec") or 0.0),
        )
        for row in rows
        if row.get("wav16")
    }

    def rms_at(name: str, start: float, end: float) -> float:
        item = wavs.get(name)
        if not item:
            return 0.0
        path, offset = item
        from app.app.services.meeting_recordings import window_rms

        return window_rms(path, start - offset, end - offset)

    return rms_at


def _load_events(settings: Settings, manifest: dict) -> list[dict]:
    rel = str(manifest.get("events") or "")
    if not rel:
        return []
    return read_events(settings.data_dir / rel)


def _write_parent_proof(
    settings: Settings,
    artifact: Artifact,
    replicas: list[dict],
    *,
    words: list[dict] | None = None,
    leaks: list[dict] | None = None,
) -> None:
    from asr.lab.pipeline import create_run

    wav = artifact_audio_path(settings, artifact.artifact_id, artifact.project_id)
    root = artifact_dir(settings, artifact.project_id, artifact.artifact_id)
    run = create_run(
        root,
        artifact_id=artifact.artifact_id,
        project_id=artifact.project_id,
        source_audio=wav,
    )
    proof = run.resolve("proof/transcript.json")
    proof.parent.mkdir(parents=True, exist_ok=True)
    payload = {"replicas": replicas, "words": words or [], "leaks": leaks or []}
    proof.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    leak_path = run.resolve("proof/leaks.json")
    leak_path.write_text(json.dumps(leaks or [], ensure_ascii=False, indent=2), encoding="utf-8")
