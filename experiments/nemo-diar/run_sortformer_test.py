"""Эксперимент: NVIDIA Streaming Sortformer v2.1 (ONNX) vs текущий pyannote.

Запуск:
  C:\\1C_NEW_SKKKK\\venvs\\nemo-diar\\Scripts\\python.exe experiments\\nemo-diar\\run_sortformer_test.py
  ... --clip-sec 600   # первые 10 минут для быстрой проверки
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GIGAAM = ROOT / "vendor" / "GigaAMGUI"
OUT = Path(__file__).resolve().parent / "out"
DEFAULT_AUDIO = ROOT / "asr" / "work" / "input_16k.wav"
PYANNOTE_BASELINE = ROOT / "asr" / "out" / "pyannote_segments.json"
CACHE = ROOT / "data" / "nemo-diar-cache"


def _setup_env() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("GIGAAM_RUNTIME_DIR", str(CACHE))
    if str(GIGAAM) not in sys.path:
        sys.path.insert(0, str(GIGAAM))


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


def _load_pyannote(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "speaker_count": data.get("speaker_count", len(data.get("speakers") or [])),
        "speakers": data.get("speakers") or [],
        "segments": [
            {"start": s["start"], "end": s["end"], "speaker": s["speaker"]}
            for s in data.get("segments") or []
        ],
        "talk_time": _talk_time(
            [{"start": s["start"], "end": s["end"], "speaker": s["speaker"]} for s in data.get("segments") or []]
        ),
    }


def _maybe_clip(audio: Path, clip_sec: float | None) -> Path:
    if not clip_sec:
        return audio
    clipped = OUT / f"clip_{int(clip_sec)}s.wav"
    if clipped.is_file() and clipped.stat().st_mtime >= audio.stat().st_mtime:
        return clipped
    import soundfile as sf

    samples, sr = sf.read(audio, dtype="float32", always_2d=True)
    keep = min(len(samples), int(clip_sec * sr))
    sf.write(clipped, samples[:keep], sr)
    return clipped


def _run_sortformer(audio: Path, provider: str, model_path: Path | None = None) -> dict:
    from src.core.diarization.sortformer_onnx import SortformerOnnxDiarizationManager

    kwargs = {"device": "auto", "provider": provider}
    if model_path is not None:
        kwargs["model_dir"] = model_path.parent
    mgr = SortformerOnnxDiarizationManager(**kwargs)
    print("Preparing Sortformer ONNX model (download on first run)...")
    mgr.prepare()
    smoke = mgr.smoke_test()
    print("ORT providers:", smoke.get("session_providers"))

    started = time.time()
    segments = mgr.diarize(str(audio))
    elapsed = time.time() - started

    payload = [
        {"start": round(seg.start, 3), "end": round(seg.end, 3), "speaker": seg.speaker}
        for seg in segments
    ]
    speakers = sorted({s["speaker"] for s in payload})
    return {
        "model": "nvidia/diar_streaming_sortformer_4spk-v2.1 (ONNX)",
        "audio": str(audio),
        "elapsed_sec": round(elapsed, 1),
        "speaker_count": len(speakers),
        "speakers": speakers,
        "segments": payload,
        "talk_time": _talk_time(payload),
        "ort": smoke,
    }


def _overlap_matrix(a_segments: list[dict], b_segments: list[dict], *, step: float = 0.5) -> dict:
    """Грубая оценка согласованности: кто говорит в одних и тех же интервалах."""
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


def _sample_turns(segments: list[dict], *, limit: int = 12) -> list[dict]:
    out: list[dict] = []
    for seg in segments:
        if seg["end"] - seg["start"] < 1.0:
            continue
        out.append(seg)
        if len(out) >= limit:
            break
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Sortformer ONNX diarization experiment")
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--baseline", type=Path, default=PYANNOTE_BASELINE)
    parser.add_argument("--provider", default="cuda", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--model-path", type=Path, default=None, help="Local ONNX file if HF CDN is blocked")
    parser.add_argument("--clip-sec", type=float, default=None, help="Optional clip length for quick test")
    args = parser.parse_args()

    if not args.audio.is_file():
        raise SystemExit(f"Audio not found: {args.audio}")

    _setup_env()
    audio = _maybe_clip(args.audio, args.clip_sec)

    print(f"Audio: {audio}")
    sortformer = _run_sortformer(audio, args.provider, args.model_path)
    (OUT / "sortformer_segments.json").write_text(
        json.dumps(sortformer, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report = {
        "audio": str(audio),
        "sortformer": {
            "speaker_count": sortformer["speaker_count"],
            "speakers": sortformer["speakers"],
            "elapsed_sec": sortformer["elapsed_sec"],
            "talk_time": sortformer["talk_time"],
            "sample_segments": _sample_turns(sortformer["segments"]),
        },
    }

    if args.baseline.is_file():
        pyannote = _load_pyannote(args.baseline)
        if args.clip_sec:
            pyannote["segments"] = [s for s in pyannote["segments"] if s["start"] < args.clip_sec]
            pyannote["speakers"] = sorted({s["speaker"] for s in pyannote["segments"]})
            pyannote["speaker_count"] = len(pyannote["speakers"])
            pyannote["talk_time"] = _talk_time(pyannote["segments"])
        report["pyannote_baseline"] = {
            "speaker_count": pyannote["speaker_count"],
            "speakers": pyannote["speakers"],
            "talk_time": pyannote["talk_time"],
            "sample_segments": _sample_turns(pyannote["segments"]),
        }
        report["overlap_matrix_sec"] = _overlap_matrix(sortformer["segments"], pyannote["segments"])

    summary_path = OUT / "report.json"
    summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Sortformer ===")
    print("Speakers:", sortformer["speaker_count"], sortformer["speakers"])
    print("Elapsed:", sortformer["elapsed_sec"], "sec")
    for row in sortformer["talk_time"]:
        print(f"  {row['speaker']}: {row['seconds']}s ({row['share']*100:.1f}%)")

    if "pyannote_baseline" in report:
        base = report["pyannote_baseline"]
        print("\n=== PyAnnote baseline ===")
        print("Speakers:", base["speaker_count"], base["speakers"])
        for row in base["talk_time"][:4]:
            print(f"  {row['speaker']}: {row['seconds']}s ({row['share']*100:.1f}%)")

    print(f"\nSaved: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
