"""Перевести артефакты с fusion/experiments статусов на voice-gate prod line."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "app.db"

FUSION_STATUSES = {"fusion_running", "experiments_running", "experiments_ready"}
TARGET_PENDING = "speakers_pending"
TARGET_CONFIRMED = "speakers_confirmed"


def _has_sortformer_match(project_id: int, artifact_id: str) -> bool:
    match = (
        ROOT
        / "data"
        / "projects"
        / str(project_id)
        / "artifacts"
        / artifact_id
        / "reports"
        / "sortformer_speaker_match.json"
    )
    return match.is_file()


def migrate() -> dict[str, int]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    stats = {"fusion_to_pending": 0, "fusion_to_confirmed": 0, "jobs_requeued": 0, "jobs_cancelled": 0}

    rows = conn.execute(
        "SELECT id, artifact_id, project_id, status FROM artifacts WHERE status IN (?, ?, ?)",
        tuple(FUSION_STATUSES),
    ).fetchall()

    for row in rows:
        new_status = TARGET_PENDING
        if _has_sortformer_match(int(row["project_id"]), row["artifact_id"]):
            new_status = TARGET_CONFIRMED
            stats["fusion_to_confirmed"] += 1
        else:
            stats["fusion_to_pending"] += 1
        conn.execute("UPDATE artifacts SET status=? WHERE id=?", (new_status, row["id"]))

    # fusion-phase jobs больше не используются
    cancelled = conn.execute(
        "UPDATE jobs SET status='error', error_message='cancelled: fusion pipeline removed', "
        "finished_at=? WHERE status IN ('queued','running') AND phase='fusion'",
        (datetime.now(timezone.utc).isoformat(),),
    )
    stats["jobs_cancelled"] = cancelled.rowcount or 0

    # speakers_confirmed без transcript — поставить ASR job, если нет активного
    confirmed = conn.execute(
        "SELECT id, artifact_id FROM artifacts WHERE status=? AND file_type='audio'",
        (TARGET_CONFIRMED,),
    ).fetchall()
    for row in confirmed:
        art_db_id = row["id"]
        has_transcript = conn.execute(
            "SELECT 1 FROM artifacts WHERE id=? AND transcription_text IS NOT NULL "
            "AND length(trim(transcription_text)) > 0",
            (art_db_id,),
        ).fetchone()
        if has_transcript:
            continue
        active = conn.execute(
            "SELECT 1 FROM jobs WHERE artifact_id=? AND status IN ('queued','running') LIMIT 1",
            (art_db_id,),
        ).fetchone()
        if active:
            continue
        conn.execute(
            "INSERT INTO jobs (artifact_id, kind, status, phase, attempts, created_at) "
            "VALUES (?, 'process_artifact', 'queued', 'asr', 0, ?)",
            (art_db_id, datetime.now(timezone.utc).isoformat()),
        )
        stats["jobs_requeued"] += 1

    conn.commit()
    conn.close()
    return stats


if __name__ == "__main__":
    result = migrate()
    print(result)
