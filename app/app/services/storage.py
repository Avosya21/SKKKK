from __future__ import annotations

import re
import secrets
from datetime import datetime
from pathlib import Path

from app.app.config import Settings

PROJECT_SUBDIRS = (
    "raw_inputs/calls",
    "raw_inputs/documents",
    "raw_inputs/notes",
    "parsed/calls",
    "parsed/documents",
    "parsed/notes",
    "structured",
    "project_notes",
    "project_state/versions",
    "tmp",
)

_SAFE_NAME = re.compile(r"[^\w\s\-().,]")


def sanitize_display_name(name: str) -> str:
    cleaned = re.sub(r"[\\/\.]+", "_", name.strip())
    cleaned = _SAFE_NAME.sub("_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:200] or "project"


def project_root(settings: Settings, project_id: int) -> Path:
    return settings.data_dir / "projects" / str(project_id)


def artifact_root(settings: Settings, project_id: int, artifact_id: str) -> Path:
    return project_root(settings, project_id) / "tmp" / artifact_id


def artifact_dir(settings: Settings, project_id: int, artifact_id: str) -> Path:
    return project_root(settings, project_id) / "artifacts" / artifact_id


def ensure_artifact_workspace(settings: Settings, project_id: int, artifact_id: str) -> Path:
    root = artifact_dir(settings, project_id, artifact_id)
    for sub in ("audio", "asr", "reports", "structured"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def artifact_audio_path(settings: Settings, artifact_id: str, project_id: int) -> Path:
    return artifact_dir(settings, project_id, artifact_id) / "audio" / "input_16k.wav"


def artifact_audio_playback_path(settings: Settings, artifact_id: str, project_id: int) -> Path:
    """Compressed AAC for HTML <audio> (phones struggle with raw WAV)."""
    return artifact_dir(settings, project_id, artifact_id) / "audio" / "playback.m4a"


def artifact_audio_download_mp3_path(settings: Settings, artifact_id: str, project_id: int) -> Path:
    """MP3 для скачивания созвона (легче WAV)."""
    return artifact_dir(settings, project_id, artifact_id) / "audio" / "download.mp3"


def artifact_transcript_path(settings: Settings, artifact_id: str, project_id: int) -> Path:
    return artifact_dir(settings, project_id, artifact_id) / "reports" / "transcript.html"


def artifact_sortformer_transcript_path(settings: Settings, artifact_id: str, project_id: int) -> Path:
    return artifact_dir(settings, project_id, artifact_id) / "reports" / "transcript_sortformer.html"


def artifact_raw_diar_path(settings: Settings, artifact_id: str, project_id: int) -> Path:
    return artifact_dir(settings, project_id, artifact_id) / "reports" / "diar_raw.html"


def artifact_two_voice_diar_path(settings: Settings, artifact_id: str, project_id: int) -> Path:
    return artifact_dir(settings, project_id, artifact_id) / "reports" / "diar_two_voice_4min.html"


def artifact_speaker_match_path(settings: Settings, artifact_id: str, project_id: int) -> Path:
    return artifact_dir(settings, project_id, artifact_id) / "reports" / "speaker_match_test.html"


def artifact_report_url(artifact_id: str) -> str:
    return f"/artifacts/{artifact_id}/report"


def sortformer_report_url(artifact_id: str) -> str:
    return f"/artifacts/{artifact_id}/report-sortformer"


def raw_diar_report_url(artifact_id: str) -> str:
    return f"/artifacts/{artifact_id}/report-diar-raw"


def artifact_audio_url(artifact_id: str) -> str:
    return f"/artifacts/{artifact_id}/audio"


def create_project_tree(settings: Settings, project_id: int) -> Path:
    root = project_root(settings, project_id)
    for sub in PROJECT_SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def generate_stored_filename(original: str) -> str:
    ext = Path(original).suffix.lower()[:16]
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    rnd = secrets.token_hex(3)
    return f"art_{stamp}_{rnd}{ext}"


def raw_input_dir(settings: Settings, project_id: int, source_type: str) -> Path:
    mapping = {
        "call": "calls",
        "document": "documents",
        "note": "notes",
        "screenshot": "documents",
    }
    sub = mapping.get(source_type, "documents")
    return project_root(settings, project_id) / "raw_inputs" / sub


def parsed_dir(settings: Settings, project_id: int, source_type: str) -> Path:
    mapping = {"call": "calls", "document": "documents", "note": "notes", "screenshot": "documents"}
    sub = mapping.get(source_type, "documents")
    return project_root(settings, project_id) / "parsed" / sub


def structured_dir(settings: Settings, project_id: int) -> Path:
    return project_root(settings, project_id) / "structured"


def project_notes_dir(settings: Settings, project_id: int) -> Path:
    return project_root(settings, project_id) / "project_notes"


def ensure_inside(base: Path, target: Path) -> Path:
    base = base.resolve()
    target = target.resolve()
    if base not in target.parents and base != target:
        raise ValueError("path escapes project directory")
    return target
