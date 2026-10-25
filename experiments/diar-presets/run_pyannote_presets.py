"""Прогон PyAnnote с несколькими пресетами resegment/clustering на эталоне.

python experiments/diar-presets/run_pyannote_presets.py
python experiments/diar-presets/run_pyannote_presets.py --only fast-switch
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "experiments" / "diar-presets" / "out"
GIGAAM_SRC = ROOT / "data" / "gigaam" / "pyannote_segments.json"
GIGAAM = "http://127.0.0.1:18102"

PRESETS: dict[str, dict[str, str]] = {
    "default": {
        "PRESET_ID": "default",
        "CLUSTER_THRESHOLD": "0.52",
        "CLUSTER_MIN_SIZE": "6",
        "RESEG_WINDOW_SEC": "1.5",
        "RESEG_HOLD_SEC": "1.2",
        "RESEG_SIM_FLOOR": "0.12",
        "DIARIZATION_MIN_SPEAKERS": "4",
        "DIARIZATION_MAX_SPEAKERS": "8",
        "SAVE_RAW": "1",
    },
    "fast-switch": {
        "PRESET_ID": "fast-switch",
        "CLUSTER_THRESHOLD": "0.48",
        "CLUSTER_MIN_SIZE": "6",
        "RESEG_WINDOW_SEC": "1.0",
        "RESEG_HOLD_SEC": "0.7",
        "RESEG_SIM_FLOOR": "0.25",
        "DIARIZATION_MIN_SPEAKERS": "4",
        "DIARIZATION_MAX_SPEAKERS": "8",
    },
    "strict-voice": {
        "PRESET_ID": "strict-voice",
        "CLUSTER_THRESHOLD": "0.50",
        "CLUSTER_MIN_SIZE": "6",
        "RESEG_WINDOW_SEC": "0.8",
        "RESEG_HOLD_SEC": "0.5",
        "RESEG_SIM_FLOOR": "0.30",
        "DIARIZATION_MIN_SPEAKERS": "4",
        "DIARIZATION_MAX_SPEAKERS": "8",
    },
    "fixed-4": {
        "PRESET_ID": "fixed-4",
        "CLUSTER_THRESHOLD": "0.52",
        "CLUSTER_MIN_SIZE": "6",
        "RESEG_WINDOW_SEC": "1.5",
        "RESEG_HOLD_SEC": "1.2",
        "RESEG_SIM_FLOOR": "0.12",
        "DIARIZATION_MIN_SPEAKERS": "4",
        "DIARIZATION_MAX_SPEAKERS": "4",
    },
}


def wait_gigaam(timeout_sec: float = 240.0) -> None:
    subprocess.run(["docker", "start", "primaryanalitics-gigaam"], check=False)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{GIGAAM}/health", timeout=5).read()
            return
        except Exception:
            time.sleep(3)
    raise SystemExit("primaryanalitics-gigaam не поднялся")


def run_preset(name: str, env: dict[str, str]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dst = OUT_DIR / f"{name}.json"
    docker_env: list[str] = []
    for key, value in env.items():
        docker_env.extend(["-e", f"{key}={value}"])
    docker_env.extend(["-e", "CLUSTER_METHOD=centroid"])
    print(f"\n=== preset {name} ===")
    proc = subprocess.run(
        [
            "docker",
            "exec",
            *docker_env,
            "primaryanalitics-gigaam",
            "python",
            "/data/dump_pyannote.py",
        ],
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"preset {name} failed: {proc.returncode}")
    if not GIGAAM_SRC.is_file():
        raise SystemExit(f"нет {GIGAAM_SRC}")
    shutil.copy2(GIGAAM_SRC, dst)
    data = json.loads(dst.read_text(encoding="utf-8"))
    n = len(data.get("segments") or [])
    sw = _switch_count(data.get("segments") or [])
    print(f"  -> {dst.name}: segments={n} switches={sw} speakers={data.get('speaker_count')}")
    return dst


def _switch_count(segments: list[dict]) -> int:
    if len(segments) < 2:
        return 0
    prev = segments[0].get("speaker")
    count = 0
    for row in segments[1:]:
        sp = row.get("speaker")
        if sp != prev:
            count += 1
            prev = sp
    return count


def write_manifest(ran: list[str]) -> Path:
    tracks: list[dict] = []
    labels = {
        "default": "PyAnnote — текущий (0.52, hold 1.2)",
        "fast-switch": "PyAnnote — быстрые переключения",
        "strict-voice": "PyAnnote — жёсткий голос",
        "fixed-4": "PyAnnote — ровно 4 спикера",
    }
    desc = {
        "default": "cluster 0.52, resegment hold 1.2 s, floor 0.12 — как в проде после фикса",
        "fast-switch": "cluster 0.48, окно 1.0 s, hold 0.7 s, floor 0.25",
        "strict-voice": "cluster 0.50, окно 0.8 s, hold 0.5 s, floor 0.30",
        "fixed-4": "min=max=4 спикера, стандартный resegment",
    }
    for name in ran:
        path = OUT_DIR / f"{name}.json"
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        tracks.append(
            {
                "id": name,
                "label": labels.get(name, name),
                "description": desc.get(name, ""),
                "file": str(path.relative_to(ROOT)).replace("\\", "/"),
                "segments": data.get("segments") or [],
            }
        )
        raw = data.get("raw_clusters")
        if raw and raw.get("segments"):
            tracks.append(
                {
                    "id": f"{name}_raw",
                    "label": f"{labels.get(name, name)} — кластеры до resegment",
                    "description": "Сырые интервалы PyAnnote до resegment_by_voice",
                    "file": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "segments": raw["segments"],
                }
            )

    sortformer = ROOT / "experiments/nemo-diar/out/pilot_22_07_26/sortformer_gguf_vulkan_input_16k_report.json"
    if sortformer.is_file():
        sf = json.loads(sortformer.read_text(encoding="utf-8"))
        tracks.append(
            {
                "id": "sortformer",
                "label": "Sortformer v2.1 GGUF",
                "description": sf.get("model") or "Sortformer offline",
                "file": str(sortformer.relative_to(ROOT)).replace("\\", "/"),
                "segments": sf.get("segments") or [],
            }
        )

    manifest = {"tracks": tracks}
    manifest_path = OUT_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nmanifest", manifest_path, "tracks", len(tracks))
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=list(PRESETS), default=None)
    parser.add_argument("--skip-docker-wait", action="store_true")
    args = parser.parse_args()

    wav = ROOT / "asr" / "work" / "input_16k.wav"
    if not wav.is_file():
        raise SystemExit(f"нет {wav} — положите эталон в asr/work/input_16k.wav")

    if not args.skip_docker_wait:
        wait_gigaam()

    names = [args.only] if args.only else list(PRESETS)
    ran: list[str] = []
    for name in names:
        run_preset(name, PRESETS[name])
        ran.append(name)

    manifest = write_manifest(ran)
    build_cmd = [
        "python",
        str(ROOT / "asr" / "build_raw_diar_view.py"),
        "--manifest",
        str(manifest),
    ]
    subprocess.run(build_cmd, cwd=str(ROOT), check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
