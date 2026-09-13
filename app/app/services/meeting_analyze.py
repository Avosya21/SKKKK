"""Queue dual-hearing jobs for native meeting tracks. No Sortformer."""

from __future__ import annotations

import json
import shutil
import wave
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.job import Job
from app.app.models.meeting import STATUS_ENDED, Meeting, MeetingParticipant
from app.app.services.meeting_merge import TIME_ROUND
from app.app.services.meeting_recordings import (
    SOURCE_MICROPHONE,
    SOURCE_SCREEN,
    events_rel,
    has_speech,
    is_human_identity,
    room_dir,
    tracks_for_meeting,
)
from app.app.services.storage import (
    artifact_dir,
    ensure_artifact_workspace,
    generate_stored_filename,
    raw_input_dir,
)
from app.app.services.upload import create_artifact_record, default_priority, hash_file

ANALYZE_FILE = "analyze.json"
KNOWN_SPEAKER_FILE = "known_speaker.json"
MEETING_FILE = "meeting.json"
MEETING_KIND = "meeting"
SOURCE_TYPE_CALL = "call"
FILE_TYPE_AUDIO = "audio"
MEETING_FILENAME = "встреча-{meeting_id}.wav"
SPEAKER_0 = "speaker_0"
WAV_HEADER_BYTES = 44

STATUS_NONE = "none"
STATUS_READY = "ready"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"

BUSY_STATUSES = {STATUS_QUEUED, STATUS_RUNNING}
RETRY_STATUSES = {STATUS_NONE, STATUS_READY, STATUS_ERROR, STATUS_DONE}
REQUEUE_STATUSES = {STATUS_ERROR, STATUS_DONE}


def known_speaker_match(name: str) -> dict:
    label = SPEAKER_0
    return {
        "source": "meeting_track",
        "merge_map": {label: label},
        "matches": {label: {"diar_label": label, "status": "confirmed", "profile_name": name}},
        "display_names": {label: name},
        "embeddings": {},
    }


def apply_known_name(payload: dict, name: str) -> dict:
    label = SPEAKER_0
    payload.setdefault("merge_map", {}).setdefault(label, label)
    row = payload.setdefault("matches", {}).setdefault(label, {"diar_label": label})
    row["status"] = "confirmed"
    row["profile_name"] = name
    payload.setdefault("display_names", {})[label] = name
    if not payload.get("source"):
        payload["source"] = "meeting_track"
    return payload


def _usable_track(data_dir: Path, room_name: str, track: dict, source: str) -> dict | None:
    identity = str(track.get("identity") or "")
    if (track.get("source") or SOURCE_MICROPHONE) != source or not is_human_identity(identity):
        return None
    rel = str(track.get("path") or "")
    if not rel:
        return None
    path = room_dir(data_dir, room_name) / rel
    if not path.is_file() or path.stat().st_size < WAV_HEADER_BYTES:
        return None
    return {**track, "abs_path": path}


def analyzable_tracks(data_dir: Path, room_name: str) -> list[dict]:
    rows: list[dict] = []
    for track in tracks_for_meeting(data_dir, room_name):
        item = _usable_track(data_dir, room_name, track, SOURCE_MICROPHONE)
        if item:
            rows.append(item)
    return rows


def screen_tracks(data_dir: Path, room_name: str) -> list[dict]:
    rows: list[dict] = []
    for track in tracks_for_meeting(data_dir, room_name):
        item = _usable_track(data_dir, room_name, track, SOURCE_SCREEN)
        if item:
            rows.append(item)
    return rows


def dt_epoch(value: datetime | None) -> float | None:
    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.timestamp()


def meeting_duration_sec(tracks: list[dict]) -> float:
    spans = [
        float(row.get("offset_sec") or 0.0) + float(row.get("duration_sec") or 0.0)
        for row in tracks
    ]
    return max(spans) if spans else 0.0


def analyze_state(settings: Settings, meeting: Meeting, db: Session | None = None) -> dict:
    tracks = analyzable_tracks(settings.data_dir, meeting.room_name)
    stored = read_analyze(settings.data_dir, meeting.room_name)
    jobs = list((stored or {}).get("jobs") or [])
    if db is not None and jobs:
        jobs = _refresh_jobs(db, jobs)
    status_name = STATUS_NONE
    if meeting.status == STATUS_ENDED and tracks and not stored:
        status_name = STATUS_READY
    elif stored:
        status_name = _jobs_status(jobs)
    can_analyze = (
        meeting.status == STATUS_ENDED and bool(tracks) and status_name in RETRY_STATUSES
    )
    if meeting.status != STATUS_ENDED:
        status_name = STATUS_NONE
        can_analyze = False
    return {
        "can_analyze": can_analyze,
        "analyze_status": status_name,
        "track_count": len(tracks),
        "analyze_jobs": jobs,
    }


def analyze_meeting(db: Session, settings: Settings, meeting: Meeting) -> Meeting:
    if meeting.status != STATUS_ENDED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Meeting is not ended")
    tracks = analyzable_tracks(settings.data_dir, meeting.room_name)
    if not tracks:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No speaker recordings")
    current = analyze_state(settings, meeting, db)
    if current["analyze_status"] in BUSY_STATUSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Analyze already running")
    stored = read_analyze(settings.data_dir, meeting.room_name)
    if stored and current["analyze_status"] in REQUEUE_STATUSES and stored.get("kind") == MEETING_KIND:
        jobs = _requeue_jobs(db, stored.get("jobs") or [])
        if not jobs:
            jobs = _enqueue_meeting(db, settings, meeting, tracks)
    else:
        jobs = _enqueue_meeting(db, settings, meeting, tracks)
    write_analyze(
        settings.data_dir,
        meeting.room_name,
        {"status": STATUS_QUEUED, "kind": MEETING_KIND, "jobs": jobs},
    )
    return meeting


def meeting_manifest_path(settings: Settings, project_id: int, artifact_id: str) -> Path:
    return artifact_dir(settings, project_id, artifact_id) / MEETING_FILE


def track_child_id(parent_id: str, identity: str) -> str:
    stem = identity.replace(":", "-")
    return f"{parent_id}__{stem}"


def inject_known_speaker_into_lab_match(settings: Settings, artifact, run_id: str) -> str | None:
    path = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    doc_path = path / KNOWN_SPEAKER_FILE
    if not doc_path.is_file():
        return None
    try:
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    name = str((doc or {}).get("speaker") or "").strip()
    if not name:
        return None
    from app.app.services.artifact_lab import resolve_run

    run = resolve_run(settings, artifact, run_id)
    match_path = run.resolve("voice/match.json")
    match_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {}
    if match_path.is_file():
        try:
            loaded = json.loads(match_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            payload = {}
    apply_known_name(payload, name)
    match_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return name


def read_analyze(data_dir: Path, room_name: str) -> dict | None:
    path = room_dir(data_dir, room_name) / ANALYZE_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_analyze(data_dir: Path, room_name: str, payload: dict) -> None:
    dest = room_dir(data_dir, room_name) / ANALYZE_FILE
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dest)


def _jobs_status(jobs: list[dict]) -> str:
    statuses = {str(row.get("status") or STATUS_QUEUED) for row in jobs}
    if not statuses:
        return STATUS_READY
    if STATUS_RUNNING in statuses:
        return STATUS_RUNNING
    if STATUS_QUEUED in statuses:
        return STATUS_QUEUED
    if STATUS_ERROR in statuses:
        return STATUS_ERROR
    if statuses <= {STATUS_DONE}:
        return STATUS_DONE
    return STATUS_QUEUED


def _refresh_jobs(db: Session, jobs: list[dict]) -> list[dict]:
    updated = []
    for row in jobs:
        job_id = row.get("job_id")
        job = db.get(Job, job_id) if job_id else None
        item = dict(row)
        if job is not None:
            item["status"] = job.status
        else:
            item["status"] = STATUS_ERROR
        updated.append(item)
    return updated


def _requeue_jobs(db: Session, jobs: list[dict]) -> list[dict]:
    from app.app.models.artifact import Artifact
    from app.app.services.queue import enqueue_process_artifact

    new_jobs: dict[str, Job] = {}
    out = []
    for row in jobs:
        key = str(row.get("artifact_id") or "")
        if key not in new_jobs:
            art = db.scalar(select(Artifact).where(Artifact.artifact_id == key))
            if art is None:
                continue
            new_jobs[key] = enqueue_process_artifact(db, art.id, phase="asr")
        job = new_jobs[key]
        item = dict(row)
        item["job_id"] = job.id
        item["status"] = job.status
        out.append(item)
    return out


def track_offset_sec(meeting_started: datetime | None, started_epoch: float | None) -> float | None:
    """Сдвиг дорожки на шкале встречи по времени первого сэмпла WAV."""
    if meeting_started is None or started_epoch is None:
        return None
    aware = meeting_started if meeting_started.tzinfo else meeting_started.replace(tzinfo=timezone.utc)
    return max(0.0, round(float(started_epoch) - aware.timestamp(), TIME_ROUND))


def _track_offset_sec(meeting: Meeting, track: dict, db: Session) -> float:
    """Манифест точнее: вход в комнату — ещё не первый сэмпл микрофона."""
    from_wav = track_offset_sec(meeting.started_at, track.get("started_epoch"))
    if from_wav is not None:
        return from_wav
    return _joined_offset_sec(meeting, str(track.get("identity") or ""), db)


def _joined_offset_sec(meeting: Meeting, identity: str, db: Session) -> float:
    row = db.scalars(
        select(MeetingParticipant)
        .where(
            MeetingParticipant.meeting_id == meeting.id,
            MeetingParticipant.identity == identity,
        )
        .order_by(MeetingParticipant.joined_at.desc())
    ).first()
    if not row or not row.joined_at or not meeting.started_at:
        return 0.0
    start = meeting.started_at
    joined = row.joined_at
    if start.tzinfo is None and joined.tzinfo is not None:
        joined = joined.replace(tzinfo=None)
    elif start.tzinfo is not None and joined.tzinfo is None:
        start = start.replace(tzinfo=None)
    return max(0.0, (joined - start).total_seconds())


def _track_contract(db: Session, meeting: Meeting, track: dict) -> dict:
    identity = str(track["identity"])
    frames = track.get("frames")
    sample_rate = int(track.get("sample_rate") or 0) or None
    duration = track.get("duration_sec")
    src = Path(track["abs_path"])
    if duration is None and frames and sample_rate:
        duration = float(frames) / float(sample_rate)
    if duration is None:
        duration = _wav_duration_sec(src)
    return {
        "identity": identity,
        "speaker": _speaker_name(db, meeting.id, identity),
        "source": track.get("source") or SOURCE_MICROPHONE,
        "src": str(src),
        "started_epoch": track.get("started_epoch"),
        "offset_sec": _track_offset_sec(meeting, track, db),
        "frames": frames,
        "sample_rate": sample_rate,
        "duration_sec": round(float(duration or 0.0), TIME_ROUND),
        "silent": not has_speech(src) if src.is_file() else True,
    }


def _enqueue_meeting(db: Session, settings: Settings, meeting: Meeting, tracks: list[dict]) -> list[dict]:
    original = MEETING_FILENAME.format(meeting_id=meeting.id)
    stored = generate_stored_filename(original)
    dest_dir = raw_input_dir(settings, meeting.project_id, SOURCE_TYPE_CALL)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / stored
    first = Path(tracks[0]["abs_path"])
    shutil.copy2(first, dest)
    sha256, _size = hash_file(dest)
    track_rows = [_track_contract(db, meeting, track) for track in tracks]
    for track in screen_tracks(settings.data_dir, meeting.room_name):
        row = _track_contract(db, meeting, track)
        row["speaker"] = "external_or_unknown"
        track_rows.append(row)
    duration = meeting_duration_sec(track_rows)
    art, job = create_artifact_record(
        db,
        project_id=meeting.project_id,
        user_id=meeting.created_by,
        original_filename=original,
        stored_filename=stored,
        file_path=dest,
        file_type=FILE_TYPE_AUDIO,
        source_type=SOURCE_TYPE_CALL,
        sha256=sha256,
        priority_weight=default_priority(SOURCE_TYPE_CALL),
        duration_sec=duration,
    )
    for row in track_rows:
        row["child_id"] = track_child_id(art.artifact_id, row["identity"])
    art.asr_engines_json = json.dumps(
        {"kind": MEETING_KIND, "meeting_id": meeting.id, "skip_sortformer": True},
        ensure_ascii=False,
    )
    db.commit()
    ws = ensure_artifact_workspace(settings, meeting.project_id, art.artifact_id)
    (ws / MEETING_FILE).write_text(
        json.dumps(
            {
                "meeting_id": meeting.id,
                "room_name": meeting.room_name,
                "meeting_started_epoch": dt_epoch(meeting.started_at),
                "meeting_ended_epoch": dt_epoch(meeting.ended_at),
                "events": events_rel(meeting.room_name),
                "duration_sec": duration,
                "tracks": track_rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return [
        {
            "identity": row["identity"],
            "speaker": row["speaker"],
            "artifact_id": art.artifact_id,
            "child_id": row["child_id"],
            "job_id": job.id,
            "status": job.status,
        }
        for row in track_rows
        if row.get("source") != SOURCE_SCREEN
    ]


def _speaker_name(db: Session, meeting_id: int, identity: str) -> str:
    row = db.scalars(
        select(MeetingParticipant)
        .where(
            MeetingParticipant.meeting_id == meeting_id,
            MeetingParticipant.identity == identity,
        )
        .order_by(MeetingParticipant.joined_at.desc())
    ).first()
    name = (row.display_name if row else "") or ""
    name = name.strip()
    return name or identity


def _wav_duration_sec(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate() or 1
            return handle.getnframes() / rate
    except wave.Error:
        return None
