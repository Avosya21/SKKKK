import json
from collections import defaultdict
from pathlib import Path

out = Path(r"D:\Primary_Analitics\experiments\diar-presets\out\two_voice_4min")
for name in ("pyannote_raw_min1", "pyannote_merge_min1", "pyannote_merge_reseg_min1", "sortformer"):
    d = json.loads((out / f"{name}.json").read_text(encoding="utf-8"))
    talk: dict[str, float] = defaultdict(float)
    segs = d["segments"]
    for s in segs:
        talk[s["speaker"]] += s["end"] - s["start"]
    total = sum(talk.values()) or 1
    sw = sum(1 for a, b in zip(segs, segs[1:]) if a["speaker"] != b["speaker"])
    print(f"\n{name}: {len(talk)} labels, {sw} switches")
    for sp, sec in sorted(talk.items(), key=lambda x: -x[1]):
        print(f"  {sp}: {sec:.0f}s ({100 * sec / total:.0f}%)")
