"""Пересчёт global integrity + plain без повторного LLM-merge.

  py scripts/rerun_transcript_integrity.py art_2026-08-22_154532_96544f
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "asr"))

from sqlalchemy import select

from app.app.config import Settings
from app.app.db import SessionLocal
from app.app.models.artifact import Artifact
from app.app.services.pipeline_audio import _asr_env, _build_transcript_view
from transcript_integrity import audit_and_repair_merge_plain, write_transcript_integrity


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/rerun_transcript_integrity.py <artifact_id>", file=sys.stderr)
        return 2

    artifact_id = sys.argv[1]
    settings = Settings()
    db = SessionLocal()
    artifact = db.scalar(select(Artifact).where(Artifact.artifact_id == artifact_id))
    if artifact is None:
        print(f"artifact not found: {artifact_id}", file=sys.stderr)
        return 1

    env = _asr_env(settings, artifact)
    asr_out = Path(env["SKKKK_ASR_OUT"])
    windows_path = asr_out / "docker_merged_windows.json"
    if not windows_path.is_file():
        print(f"missing {windows_path}", file=sys.stderr)
        return 1

    log = json.loads(windows_path.read_text(encoding="utf-8"))
    plain = "\n\n".join((e.get("text") or "").strip() for e in log if (e.get("text") or "").strip()).strip() + "\n"
    duration = float(env.get("SKKKK_AUDIO_DURATION") or 0)
    if not duration:
        meta_path = asr_out / "audio_meta.json"
        if meta_path.is_file():
            duration = float(json.loads(meta_path.read_text(encoding="utf-8")).get("duration") or 0)

    def _read(name: str) -> str:
        p = asr_out / name
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    giga_plain = _read("docker_gigaam.txt")
    tone_plain = _read("docker_tone.txt")
    whisper_plain = _read("docker_whisper.txt")

    before_chars = (asr_out / "docker_merged_plain.txt").stat().st_size if (asr_out / "docker_merged_plain.txt").is_file() else 0
    repaired, merge_integrity = audit_and_repair_merge_plain(
        plain,
        giga_plain=giga_plain,
        tone_plain=tone_plain,
        whisper_plain=whisper_plain,
        log=log,
        duration=duration,
    )
    (asr_out / "docker_merged_plain.txt").write_text(repaired, encoding="utf-8")
    write_transcript_integrity(asr_out, merge_audit=merge_integrity)

    print(
        f"integrity provider={settings.llm_provider} "
        f"plain {before_chars} -> {len(repaired)} chars "
        f"merge={merge_integrity.get('status')} "
        f"repair={merge_integrity.get('repair')}",
        flush=True,
    )

    _build_transcript_view(env)
    integrity_path = asr_out / "transcript_integrity.json"
    payload = json.loads(integrity_path.read_text(encoding="utf-8"))
    print(f"transcript_integrity status={payload.get('status')} view={payload.get('view', {}).get('status')}", flush=True)
    db.close()
    return 0 if payload.get("status") != "fail" else 1


if __name__ == "__main__":
    raise SystemExit(main())
