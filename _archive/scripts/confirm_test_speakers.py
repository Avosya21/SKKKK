"""Подтвердить спикеров по suggested_profile_id и поставить ASR в очередь."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.app.config import get_settings
from app.app.db import SessionLocal
from app.app.models.artifact import Artifact
from app.app.services.speaker_profiles import (
    confirm_speakers_and_enqueue,
    set_confirmed_links,
    speakers_confirm_status,
    sync_match_json_from_links,
    rebuild_match_report_html,
)


def confirm_with_suggestions(artifact_id: str) -> dict:
    db = SessionLocal()
    settings = get_settings()
    try:
        art = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).one()
        match_path = (
            ROOT
            / "data"
            / "projects"
            / str(art.project_id)
            / "artifacts"
            / artifact_id
            / "reports"
            / "sortformer_speaker_match.json"
        )
        payload = json.loads(match_path.read_text(encoding="utf-8"))
        mapping: dict[str, int] = {}
        for label, row in (payload.get("matches") or {}).items():
            if not isinstance(row, dict):
                continue
            pid = row.get("suggested_profile_id") or row.get("profile_id")
            if pid:
                mapping[label] = int(pid)
        if not mapping:
            raise SystemExit(f"no suggested profiles in {match_path}")

        set_confirmed_links(
            db,
            project_id=art.project_id,
            artifact_id=artifact_id,
            mapping=mapping,
        )
        sync_match_json_from_links(db, project_id=art.project_id, artifact_id=artifact_id)
        rebuild_match_report_html(db, project_id=art.project_id, artifact_id=artifact_id)
        status = speakers_confirm_status(db, project_id=art.project_id, artifact_id=artifact_id)
        print("confirm status:", status)
        if not status["ready"]:
            raise SystemExit(f"speakers not ready: pending={status['pending']}")

        if art.status != "speakers_pending":
            art.status = "speakers_pending"
            db.commit()

        result = confirm_speakers_and_enqueue(db, settings, art)
        print("queued:", result)
        return result
    finally:
        db.close()


if __name__ == "__main__":
    aid = sys.argv[1] if len(sys.argv) > 1 else "art_2026-08-26_150857_724597"
    confirm_with_suggestions(aid)
