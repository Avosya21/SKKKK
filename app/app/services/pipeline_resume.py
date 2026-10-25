"""Продолжение обработки аудио после сбоя: восстановление стенограммы из рабочей папки ASR и решение, продолжать ли."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.services.storage import artifact_dir, ensure_artifact_workspace, parsed_dir

logger = logging.getLogger("primaryanalitics.pipeline")


def _merge_step_ok(checklist_path: Path) -> bool:
    if not checklist_path.is_file():
        return False
    try:
        data = json.loads(checklist_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    merge = (data.get("steps") or {}).get("merge") or {}
    return merge.get("status") == "ok"


def try_resume_transcript(settings: Settings, artifact: Artifact) -> str | None:
    """Reuse ASR outputs when merge/view already finished (knowledge-phase retry)."""
    from app.app.services.artifact_lab import latest_run_id
    from app.app.services.storage import artifact_knowledge_text

    if latest_run_id(settings, artifact):
        text = artifact_knowledge_text(settings, artifact).strip()
        if text:
            log_event(
                logger,
                "asr.resume",
                artifact=artifact.artifact_id,
                source="lab",
                chars=len(text),
            )
            return text
        return None

    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    asr_dir = ws / "asr"
    checklist = asr_dir / "pipeline_checklist.json"
    if not _merge_step_ok(checklist):
        return None

    candidates: list[Path] = []
    if artifact.transcription_file_path:
        candidates.append(Path(artifact.transcription_file_path))
    candidates.append(parsed_dir(settings, artifact.project_id, artifact.source_type) / f"{artifact.artifact_id}_final.txt")
    candidates.extend(
        asr_dir / name
        for name in (
            "docker_merged_timecodes.txt",
            "docker_merged.txt",
            "docker_gigaam_diar_smooth_timecodes.txt",
        )
    )

    for path in candidates:
        if path.is_file() and path.stat().st_size > 50:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                log_event(
                    logger,
                    "asr.resume",
                    artifact=artifact.artifact_id,
                    source=str(path),
                    chars=len(text),
                )
                return text
    return None


def artifact_has_asr_workspace(settings: Settings, artifact: Artifact) -> bool:
    ws = artifact_dir(settings, artifact.project_id, artifact.artifact_id)
    return (ws / "asr" / "pipeline_checklist.json").is_file()


def should_continue_asr(settings: Settings, artifact: Artifact) -> bool:
    """Продолжить прерванный ASR (audio_prep уже ok, merge ещё нет)."""
    ws = artifact_dir(settings, artifact.project_id, artifact.artifact_id)
    checklist = ws / "asr" / "pipeline_checklist.json"
    if not checklist.is_file():
        return False
    try:
        data = json.loads(checklist.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    steps = data.get("steps") or {}
    if steps.get("audio_prep", {}).get("status") != "ok":
        return False
    if _merge_step_ok(checklist):
        return False
    return True
