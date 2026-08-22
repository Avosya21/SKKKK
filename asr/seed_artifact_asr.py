"""Подготовить artifacts/{id}/asr/ для build_transcript_view (без повторного ASR)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from merge_ensemble import pack_windows  # noqa: E402
from speakers import parse_timecode_lines  # noqa: E402


def _ffprobe_duration(path: Path) -> float:
    try:
        from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool

        ffprobe = resolve_ffmpeg_tool("ffprobe")
    except ImportError:
        ffprobe = "ffprobe"
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


def seed_merged_from_final(final_path: Path, asr_out: Path, duration: float) -> None:
    turns = parse_timecode_lines(final_path.read_text(encoding="utf-8"))
    if not turns:
        raise ValueError(f"нет таймкодов в {final_path}")

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


def ensure_artifact_asr_inputs(
    *,
    root: Path,
    project_id: int,
    artifact_id: str,
    ws: Path,
    match_json: Path | None = None,
) -> Path:
    """Заполнить ws/asr минимумом для build_transcript_view; вернуть каталог asr."""
    asr_out = ws / "asr"
    asr_out.mkdir(parents=True, exist_ok=True)

    parsed = root / "data" / "projects" / str(project_id) / "parsed" / "calls"
    final_txt = parsed / f"{artifact_id}_final.txt"
    summary = parsed / f"{artifact_id}_call_summary.md"
    wav = ws / "audio" / "input_16k.wav"

    merged_ok = (asr_out / "docker_merged_windows.json").is_file() and (
        asr_out / "docker_merged_plain.txt"
    ).is_file()
    if not merged_ok and final_txt.is_file() and wav.is_file():
        seed_merged_from_final(final_txt, asr_out, _ffprobe_duration(wav))
        merged_ok = True

    if not merged_ok:
        fallback = root / "asr" / "out"
        for name in (
            "docker_merged_windows.json",
            "docker_merged_plain.txt",
            "call_summary.md",
            "docker_whisper.json",
            "sortformer_segments.json",
        ):
            src = fallback / name
            if src.is_file() and not (asr_out / name).is_file():
                shutil.copy2(src, asr_out / name)

    if summary.is_file() and not (asr_out / "call_summary.md").is_file():
        shutil.copy2(summary, asr_out / "call_summary.md")

    if match_json and match_json.is_file():
        sf = asr_out / "sortformer_segments.json"
        if not sf.is_file():
            try:
                payload = json.loads(match_json.read_text(encoding="utf-8"))
                sf_src = Path(str(payload.get("sortformer_json") or ""))
                if sf_src.is_file():
                    shutil.copy2(sf_src, sf)
            except (OSError, json.JSONDecodeError, TypeError):
                pass

    return asr_out
