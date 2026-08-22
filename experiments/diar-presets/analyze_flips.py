"""Анализ переключений: raw PyAnnote vs resegment, короткие островки."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRESET = ROOT / "experiments/diar-presets/out/default.json"


def load() -> dict:
    return json.loads(PRESET.read_text(encoding="utf-8"))


def analyze(name: str, segments: list[dict], limit: float = 600.0) -> None:
    rows = [s for s in segments if s["start"] < limit]
    if not rows:
        print(f"{name}: empty")
        return
    durations = [s["end"] - s["start"] for s in rows]
    switches = sum(1 for a, b in zip(rows, rows[1:]) if a["speaker"] != b["speaker"])
    short = sum(1 for d in durations if d < 2.0)
    very_short = sum(1 for d in durations if d < 0.8)
    speakers = Counter(s["speaker"] for s in rows)
    # A-B-A pattern (flip-flop) in 15 sec window
    flips = 0
    for i in range(len(rows) - 2):
        a, b, c = rows[i], rows[i + 1], rows[i + 2]
        if a["speaker"] == c["speaker"] and a["speaker"] != b["speaker"]:
            if b["end"] - b["start"] < 3.0:
                flips += 1
    print(f"\n=== {name} (first {limit/60:.0f} min) ===")
    print(f"  segments={len(rows)} switches={switches} speakers={len(speakers)}")
    print(f"  seg<2s: {short} ({100*short/len(rows):.0f}%)  seg<0.8s: {very_short}")
    print(f"  A-B-A islands (<3s B): {flips}")
    for sp, cnt in speakers.most_common():
        sec = sum(s["end"] - s["start"] for s in rows if s["speaker"] == sp)
        print(f"    {sp}: {cnt} segs, {sec:.0f}s")


def timeline_sample(segments: list[dict], start: float = 0, end: float = 120) -> None:
    rows = [s for s in segments if s["end"] > start and s["start"] < end]
    print(f"\n--- timeline {start:.0f}-{end:.0f}s ---")
    for s in rows[:40]:
        sp = s.get("raw") or s["speaker"]
        label = s["speaker"]
        print(f"  {s['start']:6.1f}-{s['end']:6.1f}  {label:12}  raw={sp}" if "raw" in s else f"  {s['start']:6.1f}-{s['end']:6.1f}  {label}")


def main() -> None:
    data = load()
    analyze("resegment (default)", data["segments"])
    raw = data.get("raw_clusters") or {}
    analyze("raw PyAnnote clusters", raw.get("segments") or [])

    # raw cluster IDs vs display names mapping
    segs = data["segments"][:200]
    print("\n=== raw cluster -> Спикер № mapping (first 200 segs) ===")
    mapping: dict[str, Counter] = {}
    for s in segs:
        raw = s.get("raw", "?")
        mapping.setdefault(raw, Counter())[s["speaker"]] += 1
    for raw, counts in sorted(mapping.items()):
        print(f"  {raw}: {dict(counts)}")

    timeline_sample(data["segments"], 0, 180)
    timeline_sample(raw.get("segments") or [], 0, 180)


if __name__ == "__main__":
    main()
