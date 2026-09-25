"""One-off backfill: per-artifact reports + wav for done audio (no DB)."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "data" / "projects" / "1"
PARSED = PROJECT / "parsed" / "calls"
RAW = PROJECT / "raw_inputs" / "calls"


def fix_audio_src(html: str, audio_url: str) -> str:
    return re.sub(r'(<audio[^>]*\ssrc=")[^"]*(")', rf"\1{audio_url}\2", html, count=1)


def extract_wav(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(dst),
        ],
        check=True,
    )


def backfill(artifact_id: str, source_glob: str | None = None) -> None:
    ws = PROJECT / "artifacts" / artifact_id
    reports = ws / "reports"
    audio = ws / "audio"
    reports.mkdir(parents=True, exist_ok=True)
    audio.mkdir(parents=True, exist_ok=True)

    legacy = PARSED / f"{artifact_id}_transcript.html"
    if not legacy.is_file():
        print(f"skip {artifact_id}: no legacy transcript")
        return

    audio_url = f"/artifacts/{artifact_id}/audio"
    report = reports / "transcript.html"
    report.write_text(fix_audio_src(legacy.read_text(encoding="utf-8"), audio_url), encoding="utf-8")
    print(f"wrote {report}")

    wav = audio / "input_16k.wav"
    if wav.is_file():
        print(f"wav exists {wav}")
        return
    if not source_glob:
        print(f"no source_glob for {artifact_id}, wav skipped")
        return

    matches = list(RAW.glob(source_glob))
    if not matches:
        print(f"no source for {artifact_id}, wav skipped")
        return
    try:
        extract_wav(matches[0], wav)
        print(f"extracted {wav}")
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"wav extract failed for {artifact_id}: {exc}")


if __name__ == "__main__":
    backfill("art_2026-08-19_135734_674299", "*135734*")
    backfill("art_2026-08-19_211842_54f88c", "*211842*")
