"""Пересклейка merge + view для артефакта (Cursor, без ASR и без Gemma).

  python scripts/rerun_cursor_merge.py art_2026-08-22_154532_96544f
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "asr"))

from sqlalchemy import select

from app.app.config import Settings
from app.app.db import SessionLocal
from app.app.models.artifact import Artifact
from app.app.services.artifact_audio import finalize_audio_outputs, sync_transcript_view_reports
from app.app.services.pipeline_audio import (
    _asr_env,
    _build_transcript_view,
    _run_speaker_match,
    _run_voice_turn_audit,
)
from app.app.services.status import set_status
from pipeline_checklist import mark_ok, mark_pending, mark_running, print_summary


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/rerun_cursor_merge.py <artifact_id>", file=sys.stderr)
        return 2

    artifact_id = sys.argv[1]
    settings = Settings()
    if settings.llm_provider != "cursor":
        print("LLM_PROVIDER must be cursor (Gemma не используем на этом этапе)", file=sys.stderr)
        return 2
    if not settings.cursor_api_key.strip():
        print("CURSOR_API_KEY не задан", file=sys.stderr)
        return 2

    db = SessionLocal()
    artifact = db.scalar(select(Artifact).where(Artifact.artifact_id == artifact_id))
    if artifact is None:
        print(f"artifact not found: {artifact_id}", file=sys.stderr)
        return 1

    env = _asr_env(settings, artifact)
    asr_out = Path(env["SKKKK_ASR_OUT"])
    for req in ("docker_whisper.json", "docker_gigaam.json", "sortformer_segments.json"):
        if not (asr_out / req).is_file():
            print(f"missing {asr_out / req} — нужен завершённый ASR", file=sys.stderr)
            return 1

    print(f"=== cursor remerge {artifact_id} llm={settings.cursor_model} ===", flush=True)
    mark_pending("merge")
    mark_pending("view_build")

    mark_running("merge")
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "asr" / "merge_ensemble.py")],
        cwd=str(ROOT),
        env=env,
    )
    if proc.returncode != 0:
        print(f"merge_ensemble failed exit={proc.returncode}", file=sys.stderr)
        return proc.returncode
    mark_ok("merge", f"Cursor {round(time.monotonic() - t0)}s")

    mark_running("view_build")
    _build_transcript_view(env)
    _run_speaker_match(env, artifact)
    _build_transcript_view(env)
    audit = _run_voice_turn_audit(env, artifact)
    if audit.get("auto_n"):
        _build_transcript_view(env)
    mark_ok("view_build", "transcript view rebuilt")

    final_path, _url = finalize_audio_outputs(settings, artifact)
    text = final_path.read_text(encoding="utf-8")
    artifact.transcription_text = text
    artifact.transcription_file_path = str(final_path)
    sync_transcript_view_reports(settings, artifact)
    if artifact.status in {"uploaded", "preprocessing", "asr_running", "asr_completed", "error"}:
        set_status(db, artifact, "transcript_ready")
    else:
        db.commit()
    db.close()

    print_summary()
    print(f"wrote {final_path} chars={len(text)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
