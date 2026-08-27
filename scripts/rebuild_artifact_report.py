"""Пересобрать reports/transcript.html для артефакта: Sortformer + match + новый UI.

Использует уже готовую Gemma-стенogramму из parsed/calls/{id}_final.txt
(без повторного тройного ASR и склейки).

  python scripts/rebuild_artifact_report.py art_2026-08-19_135734_674299
  python scripts/rebuild_artifact_report.py art_2026-08-19_135734_674299 --skip-match
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "asr"))

from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool  # noqa: E402
from merge_ensemble import pack_windows  # noqa: E402
from speakers import Turn, parse_timecode_lines  # noqa: E402


def _ffprobe_duration(path: Path) -> float:
    ffprobe = resolve_ffmpeg_tool("ffprobe")
    out = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def _extract_wav(src: Path, dst: Path) -> None:
    ffmpeg = resolve_ffmpeg_tool("ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg,
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


def _fix_audio_src(html: str, audio_url: str) -> str:
    return re.sub(r'(<audio[^>]*\ssrc=")[^"]*(")', rf'\1{audio_url}\2', html, count=1)


def seed_merged_windows(final_path: Path, asr_out: Path, duration: float) -> None:
    """Восстановить docker_merged_windows.json из parsed final.txt."""
    turns = parse_timecode_lines(final_path.read_text(encoding="utf-8"))
    if not turns:
        raise SystemExit(f"нет таймкодов в {final_path}")

    windows = pack_windows(turns, duration)
    rows: list[dict] = []
    plain_parts: list[str] = []

    for i, (w_start, w_end) in enumerate(windows):
        parts: list[str] = []
        for turn in turns:
            if turn.start < w_end and turn.end > w_start:
                parts.append(turn.text.strip())
        text = "\n\n".join(p for p in parts if p)
        rows.append(
            {
                "window": i,
                "start": w_start,
                "end": w_end,
                "elapsed_sec": 0,
                "chars": len(text),
                "text": text,
                "skipped": not text,
            }
        )
        plain_parts.append(text)

    asr_out.mkdir(parents=True, exist_ok=True)
    (asr_out / "docker_merged_windows.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (asr_out / "docker_merged_plain.txt").write_text("\n\n".join(plain_parts), encoding="utf-8")
    print(f"seeded merged windows: {len(rows)} from {final_path.name}")


def rebuild(
    artifact_id: str,
    *,
    project_id: int = 1,
    skip_match: bool = False,
    skip_sortformer: bool = False,
) -> None:
    import sqlite3

    con = sqlite3.connect(ROOT / "data" / "app.db")
    row = con.execute(
        "select project_id, file_path from artifacts where artifact_id=?",
        (artifact_id,),
    ).fetchone()
    con.close()
    if not row:
        raise SystemExit(f"artifact not in DB: {artifact_id}")
    project_id = int(row[0])
    file_path = Path(str(row[1]))

    ws = ROOT / "data" / "projects" / str(project_id) / "artifacts" / artifact_id
    audio_dir = ws / "audio"
    asr_out = ws / "asr"
    reports = ws / "reports"
    parsed = ROOT / "data" / "projects" / str(project_id) / "parsed" / "calls"

    audio_dir.mkdir(parents=True, exist_ok=True)
    asr_out.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)

    wav = audio_dir / "input_16k.wav"
    if not wav.is_file():
        if not file_path.is_file():
            raise SystemExit(f"нет wav и исходника: {file_path}")
        print(f"extract wav from {file_path.name}")
        _extract_wav(file_path, wav)

    final_txt = parsed / f"{artifact_id}_final.txt"
    if not final_txt.is_file():
        raise SystemExit(f"нет {final_txt}")
    duration = _ffprobe_duration(wav)
    merged_windows = asr_out / "docker_merged_windows.json"
    merged_plain = asr_out / "docker_merged_plain.txt"
    if merged_windows.is_file() and merged_plain.is_file():
        print(f"keep existing merge windows: {merged_windows.name}")
    else:
        seed_merged_windows(final_txt, asr_out, duration)

    summary = parsed / f"{artifact_id}_call_summary.md"
    if summary.is_file():
        (asr_out / "call_summary.md").write_text(summary.read_text(encoding="utf-8"), encoding="utf-8")

    env = os.environ.copy()
    env["SKKKK_ASR_WORK"] = str(audio_dir)
    env["SKKKK_ASR_OUT"] = str(asr_out)
    env["SKKKK_AUDIO_URL"] = f"/artifacts/{artifact_id}/audio"
    env["SKKKK_ARTIFACT_ID"] = artifact_id
    env["SKKKK_PROJECT_ID"] = str(project_id)
    env["SKKKK_MATCH_JSON"] = str(reports / "sortformer_speaker_match.json")

    sortformer = asr_out / "sortformer_segments.json"
    if not skip_sortformer:
        print("=== Sortformer ===")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "asr" / "dump_sortformer.py")],
            cwd=str(ROOT),
            env=env,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit("dump_sortformer failed")
    elif not sortformer.is_file():
        raise SystemExit(f"--skip-sortformer but missing {sortformer}")

    match_json = reports / "sortformer_speaker_match.json"
    if not skip_match:
        print("=== speaker match ===")
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "asr" / "match_sortformer_speakers.py"),
                "--project-id",
                str(project_id),
                "--artifact-id",
                artifact_id,
                "--sortformer-json",
                str(sortformer),
                "--audio",
                str(wav),
                "--out",
                str(match_json),
            ],
            cwd=str(ROOT),
            env=env,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit("match_sortformer_speakers failed")

    print("=== build transcript HTML ===")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "asr" / "build_transcript_view.py")],
        cwd=str(ROOT),
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit("build_transcript_view failed")

    built = asr_out / "transcript.html"
    report = reports / "transcript.html"
    if not built.is_file():
        raise SystemExit(f"нет {built}")
    html = _fix_audio_src(built.read_text(encoding="utf-8"), f"/artifacts/{artifact_id}/audio")
    report.write_text(html, encoding="utf-8")
    print(f"wrote {report}")
    print(f"open http://127.0.0.1:1111/artifacts/{artifact_id}/report")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_id")
    parser.add_argument("--project-id", type=int, default=1)
    parser.add_argument("--skip-match", action="store_true")
    parser.add_argument("--skip-sortformer", action="store_true")
    args = parser.parse_args()
    rebuild(
        args.artifact_id,
        project_id=args.project_id,
        skip_match=args.skip_match,
        skip_sortformer=args.skip_sortformer,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
