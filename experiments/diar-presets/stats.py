"""Quick stats for diar presets (first 10 min)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "experiments"


def switch_stats(segments: list[dict], limit: float | None = None) -> tuple[int, int]:
    rows = segments if limit is None else [s for s in segments if s["start"] < limit]
    switches = 0
    prev = None
    for row in rows:
        sp = row["speaker"]
        if prev is not None and sp != prev:
            switches += 1
        prev = sp
    return len(rows), switches


def main() -> None:
    preset_dir = ROOT / "diar-presets" / "out"
    for name in ("default", "fast-switch", "strict-voice", "fixed-4"):
        data = json.loads((preset_dir / f"{name}.json").read_text(encoding="utf-8"))
        full_n, full_sw = switch_stats(data["segments"])
        m10_n, m10_sw = switch_stats(data["segments"], 600)
        print(
            f"{name:14} speakers={data['speaker_count']} "
            f"full: {full_n} segs / {full_sw} sw | 10m: {m10_n} segs / {m10_sw} sw"
        )
    sf_path = ROOT / "nemo-diar/out/pilot_22_07_26/sortformer_gguf_vulkan_input_16k_report.json"
    sf = json.loads(sf_path.read_text(encoding="utf-8"))
    full_n, full_sw = switch_stats(sf["segments"])
    m10_n, m10_sw = switch_stats(sf["segments"], 600)
    print(
        f"{'sortformer':14} speakers={sf['speaker_count']} "
        f"full: {full_n} segs / {full_sw} sw | 10m: {m10_n} segs / {m10_sw} sw"
    )


if __name__ == "__main__":
    main()
