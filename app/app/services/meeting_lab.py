"""Снимок лаборатории созвона: дорожка за дорожкой, входы и выходы стадий."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.services.artifact_lab import latest_run_id, resolve_run
from app.app.services.meeting_analyze import meeting_manifest_path, track_child_id
from app.app.services.meeting_checks import run_checks
from app.app.services.meeting_merge import format_meeting_clock, word_text
from app.app.services.meeting_recordings import read_events
from app.app.services.pipeline_lab import overlay_output_ready
from asr.lab.paths import LabRun

CHILD_SEP = "__"
PREVIEW_WORDS = 12
PREVIEW_REPLICAS = 8
RELOAD_MS = 4000
STATUS_MISSING = "missing"
STATUS_OK = "ok"
STATUS_LABELS = {
    "ok": "готово",
    "fail": "упало",
    "running": "идёт",
    "missing": "ждёт",
}

ONNX_STEPS: tuple[tuple[str, str], ...] = (
    ("01_audio", "WAV 16 кГц"),
    ("02_asr", "Parakeet — слова"),
    ("03_diar", "Один спикер (без Sortformer)"),
    ("04_paint", "Покраска слов"),
    ("06_transcript", "Реплики дорожки"),
)

OVERLAY_STEPS: tuple[tuple[str, str, str], ...] = (
    ("giga/giga.json", "lab_giga", "GigaAM — второй слух"),
    ("reconcile/text.json", "lab_reconcile", "Сверка lab vs GigaAM"),
    ("proof/transcript.json", "lab_proof", "Вычитка"),
)


def meeting_lab_url(artifact_id: str) -> str:
    return f"/artifacts/{artifact_id}/report-meeting-lab"


def meeting_lab_file_url(parent_id: str, child_id: str, run_id: str, relative: str) -> str:
    return f"/artifacts/{parent_id}/meeting-lab/{child_id}/lab/{run_id}/file/{relative}"


def child_belongs_to_parent(parent_id: str, child_id: str) -> bool:
    prefix = parent_id + CHILD_SEP
    return child_id.startswith(prefix) and "/" not in child_id and "\\" not in child_id


def build_meeting_lab(settings: Settings, artifact: Artifact) -> dict[str, Any]:
    meeting = _meeting_doc(settings, artifact)
    tracks = [_track_snapshot(settings, artifact, row) for row in (meeting.get("tracks") or [])]
    merge = _merge_snapshot(settings, artifact)
    events = _meeting_events(settings, meeting)
    checks = run_checks(meeting, events, merge.get("words") or [], merge.get("leaks") or [])
    return {
        "artifact_id": artifact.artifact_id,
        "filename": getattr(artifact, "original_filename", "") or artifact.artifact_id,
        "status": getattr(artifact, "status", "") or "",
        "tracks": tracks,
        "merge": merge,
        "checks": checks,
        "leaks": merge.get("leaks") or [],
        "conflicts": [row for row in (merge.get("leaks") or []) if row.get("status") == "CONFLICT"],
        "complete": bool(merge.get("replicas")),
    }


def _meeting_doc(settings: Settings, artifact: Artifact) -> dict:
    path = meeting_manifest_path(settings, artifact.project_id, artifact.artifact_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_tracks(settings: Settings, artifact: Artifact) -> list[dict]:
    return list((_meeting_doc(settings, artifact) or {}).get("tracks") or [])


def _meeting_events(settings: Settings, meeting: dict) -> list[dict]:
    rel = str(meeting.get("events") or "")
    if not rel:
        return []
    return read_events(settings.data_dir / rel)


def _track_snapshot(settings: Settings, parent: Artifact, row: dict) -> dict[str, Any]:
    identity = str(row.get("identity") or "")
    child_id = str(row.get("child_id") or track_child_id(parent.artifact_id, identity))
    child = SimpleNamespace(project_id=parent.project_id, artifact_id=child_id)
    run_id = latest_run_id(settings, child)
    stages = _track_stages(settings, parent.artifact_id, child, run_id) if run_id else []
    return {
        "identity": identity,
        "speaker": str(row.get("speaker") or identity),
        "child_id": child_id,
        "run_id": run_id,
        "stages": stages,
    }


def _track_stages(settings: Settings, parent_id: str, child, run_id: str) -> list[dict[str, Any]]:
    run = resolve_run(settings, child, run_id)
    stages = [_onnx_stage(parent_id, child.artifact_id, run, sid, title) for sid, title in ONNX_STEPS]
    stages.extend(_overlay_stage(settings, parent_id, child, run, rel, sid, title) for rel, sid, title in OVERLAY_STEPS)
    return stages


def _onnx_stage(parent_id: str, child_id: str, run: LabRun, stage_id: str, title: str) -> dict[str, Any]:
    doc = _read_json(run.root / stage_id / "stage.json") or {}
    return _stage_row(
        parent_id,
        child_id,
        run.run_id,
        stage_id,
        title,
        doc,
        preview=_onnx_preview(run, stage_id),
    )


def _overlay_stage(
    settings: Settings,
    parent_id: str,
    child,
    run: LabRun,
    relative: str,
    step_id: str,
    title: str,
) -> dict[str, Any]:
    ready = overlay_output_ready(settings, child, run.run_id, step_id)
    path = run.resolve(relative)
    doc = {
        "status": STATUS_OK if ready else STATUS_MISSING,
        "outputs": [{"name": Path(relative).name, "path": relative}] if path.is_file() else [],
        "inputs": [],
        "metrics": {},
    }
    return _stage_row(parent_id, child.artifact_id, run.run_id, step_id, title, doc, preview=_json_preview(path))


def _stage_row(
    parent_id: str,
    child_id: str,
    run_id: str,
    stage_id: str,
    title: str,
    doc: dict,
    *,
    preview: str,
) -> dict[str, Any]:
    status = str(doc.get("status") or STATUS_MISSING)
    return {
        "id": stage_id,
        "title": title,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "error": doc.get("error") or "",
        "metrics": doc.get("metrics") or {},
        "inputs": _io_links(parent_id, child_id, run_id, doc.get("inputs") or []),
        "outputs": _io_links(parent_id, child_id, run_id, doc.get("outputs") or []),
        "preview": preview,
    }


def _io_links(parent_id: str, child_id: str, run_id: str, rows: list) -> list[dict[str, str]]:
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rel = str(row.get("path") or "")
        name = str(row.get("name") or Path(rel).name or "file")
        item = {"name": name, "path": rel}
        if rel:
            item["url"] = meeting_lab_file_url(parent_id, child_id, run_id, rel)
        out.append(item)
    return out


def _onnx_preview(run: LabRun, stage_id: str) -> str:
    if stage_id == "02_asr":
        return _words_preview(run.resolve("02_asr/words.json"))
    if stage_id == "06_transcript":
        return _replicas_preview(run.resolve("06_transcript/transcript.json"))
    return ""


def _words_preview(path: Path) -> str:
    doc = _read_json(path)
    if not doc:
        return ""
    words = [str(w.get("word") or "") for w in (doc.get("words") or [])[:PREVIEW_WORDS]]
    total = int(doc.get("word_count") or len(doc.get("words") or []))
    text = " ".join(w for w in words if w)
    extra = "…" if total > PREVIEW_WORDS else ""
    return f"{total} слов: {text}{extra}".strip()


def _replicas_preview(path: Path) -> str:
    doc = _read_json(path)
    if not doc:
        return ""
    rows = list(doc.get("replicas") or [])
    lines = [f"{r.get('speaker')}: {r.get('text')}" for r in rows[:PREVIEW_REPLICAS]]
    extra = "…" if len(rows) > PREVIEW_REPLICAS else ""
    return "\n".join(lines) + extra


def _json_preview(path: Path) -> str:
    name = path.name
    if name == "transcript.json":
        return _replicas_preview(path)
    if name == "words.json":
        return _words_preview(path)
    doc = _read_json(path)
    if not doc:
        return ""
    keys = ", ".join(list(doc.keys())[:8])
    return f"ключи: {keys}" if keys else ""


def _merge_snapshot(settings: Settings, artifact: Artifact) -> dict[str, Any]:
    run_id = latest_run_id(settings, artifact)
    if not run_id:
        return {
            "status": STATUS_MISSING,
            "status_label": STATUS_LABELS[STATUS_MISSING],
            "replicas": [],
            "words": [],
            "preview": "",
        }
    run = resolve_run(settings, artifact, run_id)
    path = run.resolve("proof/transcript.json")
    doc = _read_json(path) or {}
    replicas = [_with_clock(row) for row in (doc.get("replicas") or [])]
    words = [_with_clock(row) for row in (doc.get("words") or [])]
    leaks = list(doc.get("leaks") or [])
    status = STATUS_OK if replicas or words else STATUS_MISSING
    return {
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "run_id": run_id,
        "replicas": replicas,
        "words": words,
        "leaks": leaks,
        "preview": _replicas_preview(path),
    }


def _with_clock(row: dict) -> dict[str, Any]:
    item = dict(row)
    item["clock"] = format_meeting_clock(float(row.get("start") or 0.0))
    item["text"] = word_text(row)
    return item


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
