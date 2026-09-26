"""Анализ текущего pyannote baseline без Sortformer."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "asr" / "out" / "pyannote_segments.json"
OUT = Path(__file__).resolve().parent / "out" / "pyannote_analysis.json"


def main() -> None:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    segments = data.get("segments") or []
    max_t = max((s["end"] for s in segments), default=0)

    switches = 0
    prev = None
    for seg in segments:
        if prev and seg["speaker"] != prev:
            switches += 1
        prev = seg["speaker"]

    by_speaker = defaultdict(float)
    for seg in segments:
        by_speaker[seg["speaker"]] += seg["end"] - seg["start"]

    # первые 10 минут
    first10 = [s for s in segments if s["start"] < 600]
    spk10 = sorted({s["speaker"] for s in first10})
    switches10 = sum(
        1 for i in range(1, len(first10)) if first10[i]["speaker"] != first10[i - 1]["speaker"]
    )

    short_turns = [
        s for s in segments if 0.5 <= (s["end"] - s["start"]) <= 5.0
    ]
    short_by_speaker = defaultdict(int)
    for s in short_turns:
        short_by_speaker[s["speaker"]] += 1

    report = {
        "audio_duration_sec": round(max_t, 1),
        "speaker_count": data.get("speaker_count"),
        "speakers": data.get("speakers"),
        "segment_count": len(segments),
        "speaker_switches_total": switches,
        "first_10_min": {
            "speakers": spk10,
            "speaker_count": len(spk10),
            "segments": len(first10),
            "switches": switches10,
            "sample": first10[:15],
        },
        "talk_time_sec": {k: round(v, 1) for k, v in sorted(by_speaker.items(), key=lambda x: -x[1])},
        "short_turns_0_5_to_5s": dict(short_by_speaker),
        "notes": [
            "PyAnnote нашёл 7 спикеров; Sortformer ONNX ограничен 4.",
            "Спикеры №2 и №3 — очень мало речи (~1.3%), возможны короткие реплики/перебивания.",
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
