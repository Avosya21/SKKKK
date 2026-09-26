"""Переописать кадры шэринга экрана прогона vision-моделью и пересобрать прод-HTML.

Использует уже снятые кадры (frames/*.jpg), не трогая исходное видео.

  python scripts/redescribe_screen_frames.py art_2026-09-16_144208_f61d74
  python scripts/redescribe_screen_frames.py art_2026-09-16_144208_f61d74 --run 20260916T124903Z_33928e
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "asr"))

from app.app.config import get_settings  # noqa: E402
from app.app.db import SessionLocal  # noqa: E402
from app.app.models.artifact import Artifact  # noqa: E402
from app.app.services.artifact_lab import resolve_run  # noqa: E402
from app.app.services.meeting_frames import redescribe_existing_frames  # noqa: E402
from app.app.services.pipeline_lab import (  # noqa: E402
    _load_proof_replicas,
    load_run_screen_segments,
    replicas_to_turn_words,
    replicas_to_turns,
    write_prod_view,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_id")
    parser.add_argument("--run", default=None, help="run_id; по умолчанию latest")
    parser.add_argument("--max-n", type=int, default=None, help="сколько кадров описать (для теста)")
    parser.add_argument("--rebuild-only", action="store_true", help="только пересобрать HTML, без переописания кадров")
    args = parser.parse_args()

    settings = get_settings()
    db = SessionLocal()
    try:
        art = db.query(Artifact).filter(Artifact.artifact_id == args.artifact_id).one()
    except Exception as exc:
        print(f"artifact not found: {args.artifact_id}: {exc}")
        return 1
    try:
        run = resolve_run(settings, art, args.run)
    except FileNotFoundError as exc:
        print(f"run error: {exc}")
        return 1

    print(f"artifact={art.artifact_id} run={run.run_id}")
    if not args.rebuild_only:
        updated = redescribe_existing_frames(settings, run, max_n=args.max_n)
        print(f"redescribed frames: {updated}")
        if not updated:
            return 0

    run_id = args.run or run.run_id
    replicas = _load_proof_replicas(settings, art, run_id)
    turns = replicas_to_turns(replicas)
    turn_words = replicas_to_turn_words(replicas)
    screen_segments = load_run_screen_segments(settings, art, run_id)
    from app.app.services.storage import artifact_dir  # noqa: PLC0415

    ws = artifact_dir(settings, art.project_id, art.artifact_id)
    match_path = ws / "reports" / "sortformer_speaker_match.json"
    match_payload = None
    import json as _json  # noqa: PLC0415
    if match_path.is_file():
        match_payload = _json.loads(match_path.read_text(encoding="utf-8"))
    write_prod_view(
        ws / "asr",
        turns,
        match_payload=match_payload,
        artifact_id=art.artifact_id,
        project_id=art.project_id,
        screen_segments=screen_segments,
        turn_words=turn_words,
    )
    from app.app.services.artifact_audio import sync_transcript_view_reports  # noqa: PLC0415

    sync_transcript_view_reports(settings, art)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())