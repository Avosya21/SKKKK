"""Ultra Diar Streaming Sortformer 8spk v1 (NeMo) vs Sortformer 4spk GGUF / PyAnnote.

Example:
  D:\\Primary_Analitics\\venvs\\nemo-diar\\Scripts\\python.exe experiments\\nemo-diar\\run_ultra_8spk_test.py
  ... --clip-sec 240
  ... --audio D:\\Primary_Analitics\\asr\\work\\input_16k.wav
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "out"
DEFAULT_AUDIO = ROOT / "asr" / "work" / "input_16k.wav"
PYANNOTE_BASELINE = ROOT / "asr" / "out" / "pyannote_segments.json"
SORTFORMER_4SPK = (
    ROOT / "experiments" / "nemo-diar" / "out" / "pilot_22_07_26" / "sortformer_gguf_vulkan_input_16k_report.json"
)
MODEL_ID = "devsy0117/ultra_diar_streaming_sortformer_8spk_v1"
DEFAULT_NEMO = ROOT / "data" / "nemo-diar-cache" / "ultra_8spk_v1" / "ultra_diar_streaming_sortformer_8spk_v1.nemo"


def _talk_time(segments: list[dict]) -> list[dict]:
    totals: dict[str, float] = defaultdict(float)
    for seg in segments:
        totals[seg["speaker"]] += max(0.0, float(seg["end"]) - float(seg["start"]))
    total = sum(totals.values()) or 1.0
    rows = [
        {"speaker": sp, "seconds": round(sec, 1), "share": round(sec / total, 3)}
        for sp, sec in totals.items()
    ]
    rows.sort(key=lambda r: r["seconds"], reverse=True)
    return rows


def _maybe_clip(audio: Path, clip_sec: float | None) -> Path:
    if not clip_sec:
        return audio
    clipped = OUT / f"ultra8_clip_{int(clip_sec)}s.wav"
    if clipped.is_file() and clipped.stat().st_mtime >= audio.stat().st_mtime:
        return clipped
    import soundfile as sf

    samples, sr = sf.read(audio, dtype="float32", always_2d=True)
    keep = min(len(samples), int(clip_sec * sr))
    sf.write(clipped, samples[:keep], sr)
    return clipped


def _parse_segment_line(line: str) -> dict | None:
    text = str(line).strip()
    if not text:
        return None
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*(?:s|sec)?\s*(?:speaker|spk)[_\s-]*(\d+)",
        text,
        flags=re.I,
    )
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?).*?(?:SPEAKER|spk)[_\s-]*(\d+)", text, flags=re.I)
    if not m:
        return None
    spk = int(m.group(3))
    return {
        "start": round(float(m.group(1)), 3),
        "end": round(float(m.group(2)), 3),
        "speaker": f"SPEAKER_{spk:02d}",
    }


def _normalize_segments(raw_segments) -> list[dict]:
    out: list[dict] = []
    for item in raw_segments or []:
        if isinstance(item, dict):
            start = item.get("start", item.get("begin"))
            end = item.get("end", item.get("stop"))
            spk = item.get("speaker", item.get("label", item.get("spk")))
            if start is None or end is None or spk is None:
                continue
            if isinstance(spk, int):
                spk = f"SPEAKER_{spk:02d}"
            elif isinstance(spk, str) and spk.isdigit():
                spk = f"SPEAKER_{int(spk):02d}"
            out.append({"start": round(float(start), 3), "end": round(float(end), 3), "speaker": str(spk)})
            continue
        parsed = _parse_segment_line(str(item))
        if parsed:
            out.append(parsed)
    out.sort(key=lambda s: (s["start"], s["end"], s["speaker"]))
    return out


def _load_baseline(path: Path, clip_sec: float | None) -> dict | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = [
        {"start": s["start"], "end": s["end"], "speaker": s["speaker"]}
        for s in data.get("segments") or []
    ]
    if clip_sec:
        segments = [s for s in segments if s["start"] < clip_sec]
    speakers = sorted({s["speaker"] for s in segments})
    return {
        "model": data.get("model", path.name),
        "speaker_count": len(speakers),
        "speakers": speakers,
        "segment_count": len(segments),
        "talk_time": _talk_time(segments),
        "sample_segments": segments[:12],
    }


def _overlap_matrix(a_segments: list[dict], b_segments: list[dict], *, step: float = 0.5) -> dict:
    if not a_segments or not b_segments:
        return {}
    end = max(max(s["end"] for s in a_segments), max(s["end"] for s in b_segments))
    a_spk = sorted({s["speaker"] for s in a_segments})
    b_spk = sorted({s["speaker"] for s in b_segments})
    matrix = {f"{as_}|{bs}": 0.0 for as_ in a_spk for bs in b_spk}
    t = 0.0
    while t < end:
        a_here = {s["speaker"] for s in a_segments if s["start"] <= t < s["end"]}
        b_here = {s["speaker"] for s in b_segments if s["start"] <= t < s["end"]}
        for as_ in a_here:
            for bs in b_here:
                matrix[f"{as_}|{bs}"] += step
        t += step
    return matrix


def _resolve_model_path(model_path: Path | None) -> Path | None:
    if model_path and model_path.is_file():
        return model_path
    if DEFAULT_NEMO.is_file():
        return DEFAULT_NEMO
    return model_path if model_path else None


def _run_ultra_8spk(audio: Path, *, device: str, model_path: Path | None = None) -> dict:
    import torch
    from nemo.collections.asr.models import SortformerEncLabelModel

    if device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("CUDA requested but not available")

    map_location = torch.device(device)
    local_nemo = _resolve_model_path(model_path)
    print(f"Loading Ultra 8spk on {device}...")
    t0 = time.time()
    if local_nemo and local_nemo.is_file():
        print(f"  from local: {local_nemo}")
        diar_model = SortformerEncLabelModel.restore_from(
            restore_path=str(local_nemo),
            map_location=map_location,
            strict=False,
        )
        model_label = f"{MODEL_ID} (local {local_nemo.name})"
    else:
        print(f"  from Hugging Face: {MODEL_ID}")
        diar_model = SortformerEncLabelModel.from_pretrained(MODEL_ID, map_location=map_location)
        model_label = MODEL_ID
    diar_model.eval()
    if device == "cuda":
        diar_model = diar_model.cuda()
    load_sec = round(time.time() - t0, 1)
    print(f"Model loaded in {load_sec}s")

    diar_model.sortformer_modules.chunk_len = 340
    diar_model.sortformer_modules.chunk_right_context = 40
    diar_model.sortformer_modules.fifo_len = 40
    diar_model.sortformer_modules.spkcache_update_period = 300

    started = time.time()
    predicted = diar_model.diarize(audio=[str(audio)], batch_size=1)
    elapsed = round(time.time() - started, 1)
    segments = _normalize_segments(predicted[0] if predicted else [])
    speakers = sorted({s["speaker"] for s in segments})
    switches = sum(1 for a, b in zip(segments, segments[1:]) if a["speaker"] != b["speaker"])
    return {
        "model": model_label,
        "audio": str(audio),
        "device": device,
        "load_sec": load_sec,
        "elapsed_sec": elapsed,
        "speaker_count": len(speakers),
        "speakers": speakers,
        "segment_count": len(segments),
        "speaker_switches": switches,
        "segments": segments,
        "talk_time": _talk_time(segments),
        "sample_segments": segments[:12],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Ultra Sortformer 8spk v1 test")
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--clip-sec", type=float, default=240.0, help="0 = full file")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument(
        "--model-path",
        type=Path,
        default=DEFAULT_NEMO,
        help="Local .nemo file; if missing, tries Hugging Face download",
    )
    parser.add_argument("--baseline-pyannote", type=Path, default=PYANNOTE_BASELINE)
    parser.add_argument("--baseline-sortformer4", type=Path, default=SORTFORMER_4SPK)
    args = parser.parse_args()

    if not args.audio.is_file():
        raise SystemExit(f"Audio not found: {args.audio}")

    OUT.mkdir(parents=True, exist_ok=True)
    clip = None if args.clip_sec <= 0 else args.clip_sec
    audio = _maybe_clip(args.audio, clip)

    print(f"Audio: {audio}")
    ultra = _run_ultra_8spk(audio, device=args.device, model_path=args.model_path)

    tag = "full" if clip is None else f"{int(clip)}s"
    out_json = OUT / f"ultra_8spk_v1_{tag}_segments.json"
    out_json.write_text(json.dumps(ultra, ensure_ascii=False, indent=2), encoding="utf-8")

    report: dict = {
        "audio": str(audio),
        "clip_sec": clip,
        "ultra_8spk_v1": {
            k: ultra[k]
            for k in (
                "model",
                "device",
                "load_sec",
                "elapsed_sec",
                "speaker_count",
                "speakers",
                "segment_count",
                "speaker_switches",
                "talk_time",
                "sample_segments",
            )
        },
    }

    py = _load_baseline(args.baseline_pyannote, clip)
    if py:
        report["pyannote_baseline"] = py
        py_segments = json.loads(args.baseline_pyannote.read_text(encoding="utf-8")).get("segments") or []
        py_segments = [{"start": s["start"], "end": s["end"], "speaker": s["speaker"]} for s in py_segments]
        if clip:
            py_segments = [s for s in py_segments if s["start"] < clip]
        report["overlap_ultra_vs_pyannote_sec"] = _overlap_matrix(ultra["segments"], py_segments)

    sf4 = _load_baseline(args.baseline_sortformer4, clip)
    if sf4:
        report["sortformer_4spk_gguf"] = sf4
        sf4_segments = [
            {"start": s["start"], "end": s["end"], "speaker": s["speaker"]}
            for s in json.loads(args.baseline_sortformer4.read_text(encoding="utf-8")).get("segments") or []
        ]
        if clip:
            sf4_segments = [s for s in sf4_segments if s["start"] < clip]
        report["overlap_ultra_vs_sortformer4_sec"] = _overlap_matrix(ultra["segments"], sf4_segments)

    report_path = OUT / f"ultra_8spk_v1_{tag}_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Ultra 8spk v1 ===")
    print("Speakers:", ultra["speaker_count"], ultra["speakers"])
    print("Segments:", ultra["segment_count"], "switches:", ultra["speaker_switches"])
    print("Load:", ultra["load_sec"], "s diarize:", ultra["elapsed_sec"], "s")
    for row in ultra["talk_time"]:
        print(f"  {row['speaker']}: {row['seconds']}s ({row['share']*100:.1f}%)")

    if py:
        print("\n=== PyAnnote baseline ===")
        print("Speakers:", py["speaker_count"], py["speakers"])
    if sf4:
        print("\n=== Sortformer 4spk GGUF (pilot) ===")
        print("Speakers:", sf4["speaker_count"], sf4["speakers"])

    print(f"\nSaved: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
