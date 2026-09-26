"""Parse OpenResearchTools Engine span markdown into JSON report."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

PAT = re.compile(r"### (SPEAKER_\d+) \[(\d\d):(\d\d)\.(\d+) - (\d\d):(\d\d)\.(\d+)\]")


def _ts(m: str, s: str, ms: str) -> float:
    return int(m) * 60 + int(s) + int(ms) / 1000


def parse_spans(path: Path) -> list[dict]:
    segments: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PAT.match(line.strip())
        if not match:
            continue
        spk, sm, ss, sms, em, es, ems = match.groups()
        start = _ts(sm, ss, sms)
        end = _ts(em, es, ems)
        if end <= start:
            continue
        segments.append({"start": round(start, 3), "end": round(end, 3), "speaker": spk})
    return segments


def talk_time(segments: list[dict]) -> list[dict]:
    totals: dict[str, float] = defaultdict(float)
    for seg in segments:
        totals[seg["speaker"]] += seg["end"] - seg["start"]
    total = sum(totals.values()) or 1.0
    rows = [
        {"speaker": sp, "seconds": round(sec, 1), "share": round(sec / total, 3)}
        for sp, sec in totals.items()
    ]
    rows.sort(key=lambda row: row["seconds"], reverse=True)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spans", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--elapsed-sec", type=float, default=0.0)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--clip-sec", type=float, default=None)
    args = parser.parse_args()

    segments = parse_spans(args.spans)
    totals = talk_time(segments)
    report = {
        "model": "openresearchtools/diar_streaming_sortformer_4spk-v2.1-gguf",
        "audio": str(args.audio),
        "elapsed_sec": args.elapsed_sec,
        "speaker_count": len({s["speaker"] for s in segments}),
        "speakers": sorted({s["speaker"] for s in segments}),
        "segment_count": len(segments),
        "talk_time": totals,
        "sample_segments": [s for s in segments if s["end"] - s["start"] >= 2.0][:15],
        "segments": segments,
    }

    if args.baseline and args.baseline.is_file():
        base = json.loads(args.baseline.read_text(encoding="utf-8"))
        clip = args.clip_sec
        bsegs = base["segments"] if clip is None else [s for s in base["segments"] if s["start"] < clip]
        report["pyannote_baseline"] = {
            "speaker_count": len({s["speaker"] for s in bsegs}),
            "segment_count": len(bsegs),
            "talk_time": talk_time(bsegs)[:6],
            "sample_segments": [
                {"start": round(s["start"], 1), "end": round(s["end"], 1), "speaker": s["speaker"]}
                for s in bsegs
                if s["end"] - s["start"] >= 2
            ][:10],
        }

    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {args.out}")
    print("Speakers:", report["speaker_count"], report["speakers"])
    for row in report["talk_time"]:
        print(f"  {row['speaker']}: {row['seconds']}s ({row['share']*100:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
