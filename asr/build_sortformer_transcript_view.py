"""Пробная стенограмма: текст ASR + интервалы Sortformer v2.1 (GGUF).

python asr/build_sortformer_transcript_view.py
python asr/build_sortformer_transcript_view.py --sortformer-json experiments/nemo-diar/out/pilot_22_07_26/sortformer_gguf_vulkan_input_16k_report.json --out data/projects/1/artifacts/art_2026-08-19_211842_54f88c/reports/transcript_sortformer.html --audio-url /artifacts/art_2026-08-19_211842_54f88c/audio
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASR = ROOT / "asr"
sys.path.insert(0, str(ASR))

from speakers import (  # noqa: E402
    Turn,
    apply_merged_windows,
    carve_text_by_speakers,
    merge_adjacent,
    parse_timecode_lines,
    prepare_speaker_timeline,
    reattach_cut_letters,
    renumber_speakers,
)
from transcript_view import load_call_summary_files, write_transcript_html  # noqa: E402

from paths import OUT  # noqa: E402


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _gigaam_text_turns(asr_out: Path) -> list[Turn]:
    diar_tc = asr_out / "docker_gigaam_diar_timecodes.txt"
    if not diar_tc.is_file():
        raise SystemExit(f"нет {diar_tc}")
    return [
        Turn(start=t.start, end=t.end, speaker="", text=t.text)
        for t in parse_timecode_lines(_read(diar_tc))
    ]


def _merged_windows(asr_out: Path) -> list[tuple[float, float, str]]:
    log_path = asr_out / "docker_merged_windows.json"
    plain = asr_out / "docker_merged_plain.txt"
    if not log_path.is_file() or not plain.is_file():
        return []
    log = json.loads(log_path.read_text(encoding="utf-8"))
    rows = [row for row in log if not row.get("skipped")]
    if not rows:
        return []
    if all(isinstance(row.get("text"), str) and row["text"].strip() for row in rows):
        return [(float(row["start"]), float(row["end"]), row["text"].strip()) for row in rows]
    body = _read(plain)
    parts: list[str] = []
    pos = 0
    for i, row in enumerate(rows):
        n = int(row.get("chars") or 0)
        if n <= 0:
            return []
        chunk = body[pos : pos + n]
        parts.append(chunk.strip())
        pos += n
        if i < len(rows) - 1 and body[pos : pos + 2] == "\n\n":
            pos += 2
    if len(parts) != len(rows):
        return []
    return [(float(row["start"]), float(row["end"]), text) for row, text in zip(rows, parts)]


def load_sortformer_segments(path: Path) -> list[Turn]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("segments") if isinstance(data, dict) else data
    turns: list[Turn] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        speaker = str(row.get("speaker") or "").strip() or "SPEAKER_00"
        start = float(row.get("start") or 0.0)
        end = float(row.get("end") or start)
        if end <= start:
            continue
        turns.append(Turn(start=start, end=end, speaker=speaker, text=""))
    return turns


def build(
    *,
    sortformer_json: Path,
    asr_out: Path,
    out_html: Path,
    audio_url: str,
    summary_path: Path | None = None,
) -> dict:
    raw = load_sortformer_segments(sortformer_json)
    if not raw:
        raise SystemExit(f"пустые сегменты Sortformer: {sortformer_json}")

    speaker_turns = merge_adjacent(raw, gap_sec=0.25)
    speaker_turns = prepare_speaker_timeline(speaker_turns, min_seconds=15.0)
    speaker_turns = renumber_speakers(speaker_turns)

    windows = _merged_windows(asr_out)
    gigaam_chunks = _gigaam_text_turns(asr_out)
    if windows:
        turns = apply_merged_windows(speaker_turns, windows)
        source = "Sortformer + склейка Local LLM (окна)"
    else:
        turns = carve_text_by_speakers(gigaam_chunks, speaker_turns)
        source = "Sortformer + carve GigaAM"

    turns = reattach_cut_letters(merge_adjacent(turns))
    from hallucination_guard import sanitize_transcript_turns

    turns = sanitize_transcript_turns(turns)

    summary_md, brief_md = ("", "")
    if summary_path and summary_path.is_file():
        summary_md, brief_md = load_call_summary_files(summary_path.parent)
        summary_md = _read(summary_path)
    write_transcript_html(
        out_html,
        title="Созвон — склейка трёх ASR, нарезка по голосам Sortformer v2.1",
        audio_url=audio_url,
        turns=turns,
        mapping=None,
        summary_md=summary_md,
        brief_md=brief_md,
    )
    return {
        "ok": True,
        "source": source,
        "html": str(out_html),
        "turns": len(turns),
        "speakers": sorted({t.speaker for t in turns}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="HTML-просмотр стенogramмы с Sortformer")
    parser.add_argument(
        "--sortformer-json",
        type=Path,
        default=ROOT / "experiments/nemo-diar/out/pilot_22_07_26/sortformer_gguf_vulkan_input_16k_report.json",
    )
    parser.add_argument("--asr-out", type=Path, default=OUT)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT
        / "data/projects/1/artifacts/art_2026-08-19_211842_54f88c/reports/transcript_sortformer.html",
    )
    parser.add_argument(
        "--audio-url",
        default="/artifacts/art_2026-08-19_211842_54f88c/audio",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=ROOT / "data/projects/1/parsed/calls/art_2026-08-19_211842_54f88c_call_summary.md",
    )
    args = parser.parse_args()
    summary = build(
        sortformer_json=args.sortformer_json,
        asr_out=args.asr_out,
        out_html=args.out,
        audio_url=args.audio_url,
        summary_path=args.summary,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
