"""Диагностика двух голосов (M+F) на первых 4 минутах эталона.

python experiments/diar-presets/run_two_voice_4min.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "diar-presets" / "out" / "two_voice_4min"
CLIP = ROOT / "asr" / "work" / "input_16k_4min.wav"
SRC_WAV = ROOT / "asr" / "work" / "input_16k.wav"
GIGAAM_SRC = ROOT / "data" / "gigaam" / "pyannote_segments.json"
GIGAAM = "http://127.0.0.1:18102"
CLIP_SEC = 240.0
ARTIFACT = "art_2026-08-19_211842_54f88c"


def extract_clip() -> None:
    if not SRC_WAV.is_file():
        raise SystemExit(f"нет {SRC_WAV}")
    CLIP.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = ROOT / ".." / "ffmpeg"
    candidates = [
        Path(r"C:\ffmpeg-8.0-full_build\bin\ffmpeg.exe"),
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
    ]
    ffmpeg_bin = next((p for p in candidates if p.is_file()), None)
    if ffmpeg_bin is None:
        import shutil

        found = shutil.which("ffmpeg")
        if not found:
            raise SystemExit("ffmpeg not found")
        ffmpeg_bin = Path(found)
    subprocess.run(
        [
            str(ffmpeg_bin),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(SRC_WAV),
            "-t",
            str(int(CLIP_SEC)),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(CLIP),
        ],
        check=True,
    )
    print(f"clip {CLIP} ({CLIP_SEC:.0f}s)")


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


def docker_diar(name: str, env: dict[str, str]) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    dst = OUT / f"{name}.json"
    docker_env: list[str] = []
    for key, value in env.items():
        docker_env.extend(["-e", f"{key}={value}"])
    docker_env.extend(
        [
            "-e",
            "PYANNOTE_AUDIO=/work/input_16k_4min.wav",
            "-e",
            "CLUSTER_METHOD=centroid",
            "-e",
            "CLUSTER_THRESHOLD=0.52",
            "-e",
            "CLUSTER_MIN_SIZE=6",
            "-e",
            "DIARIZATION_MIN_SPEAKERS=1",
            "-e",
            "DIARIZATION_MAX_SPEAKERS=8",
            "-e",
            "SAVE_RAW=1",
        ]
    )
    print(f"\n=== {name} ===")
    proc = subprocess.run(
        ["docker", "exec", *docker_env, "primaryanalitics-gigaam", "python", "/data/dump_pyannote.py"],
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"{name} failed: {proc.returncode}")
    if not GIGAAM_SRC.is_file():
        raise SystemExit(f"нет {GIGAAM_SRC}")
    shutil.copy2(GIGAAM_SRC, dst)
    data = json.loads(dst.read_text(encoding="utf-8"))
    sw = _switches(data.get("segments") or [])
    print(f"  speakers={data.get('speaker_count')} segments={len(data.get('segments') or [])} switches={sw}")
    print(f"  source={data.get('source')}")
    return dst


def _switches(segments: list[dict]) -> int:
    if len(segments) < 2:
        return 0
    n = 0
    prev = segments[0]["speaker"]
    for row in segments[1:]:
        if row["speaker"] != prev:
            n += 1
            prev = row["speaker"]
    return n


def run_sortformer() -> Path | None:
    ps1 = ROOT / "experiments" / "nemo-diar" / "run_sortformer_gguf.ps1"
    if not ps1.is_file():
        print("sortformer: skip, no script")
        return None
    sf_out = OUT / "sortformer"
    sf_out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ps1),
            "-Audio",
            str(CLIP),
            "-ClipSec",
            "0",
            "-OutDir",
            str(sf_out),
            "-Prefer",
            "vulkan",
        ],
        check=False,
    )
    if proc.returncode != 0:
        print(f"sortformer failed: {proc.returncode}")
        return None
    reports = sorted(sf_out.glob("sortformer_gguf_*_report.json"))
    if not reports:
        return None
    dst = OUT / "sortformer.json"
    shutil.copy2(reports[-1], dst)
    data = json.loads(dst.read_text(encoding="utf-8"))
    sw = _switches(data.get("segments") or [])
    print(f"\n=== sortformer ===\n  speakers={data.get('speaker_count')} segments={len(data.get('segments') or [])} switches={sw}")
    return dst


def write_manifest(paths: list[tuple[str, Path, str]]) -> Path:
    tracks = []
    for track_id, path, description in paths:
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        tracks.append(
            {
                "id": track_id,
                "label": description,
                "description": data.get("source") or description,
                "file": str(path.relative_to(ROOT)).replace("\\", "/"),
                "segments": data.get("segments") or [],
            }
        )
    manifest = {"tracks": tracks, "clip_sec": CLIP_SEC, "note": "Первые 4 мин — два голоса (M+F)"}
    manifest_path = OUT / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def build_html(manifest_path: Path) -> None:
    report = ROOT / "data" / "projects" / "1" / "artifacts" / ARTIFACT / "reports" / "diar_two_voice_4min.html"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "asr" / "build_raw_diar_view.py"),
            "--manifest",
            str(manifest_path),
            "--out",
            str(report),
            "--audio-url",
            f"/artifacts/{ARTIFACT}/audio?clip=240",
        ],
        cwd=str(ROOT),
        check=True,
    )


def print_timelines(paths: list[tuple[str, Path]]) -> None:
    print("\n=== timelines 0-240s (sample) ===")
    for name, path in paths:
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("segments") or []
        print(f"\n--- {name} ---")
        for row in rows[:25]:
            print(f"  {row['start']:6.1f}-{row['end']:6.1f}  {row['speaker']}")


def main() -> int:
    extract_clip()
    wait_gigaam()

    variants = [
        (
            "pyannote_raw_min1",
            {
                "PRESET_ID": "pyannote_raw_min1",
                "SKIP_RESEGMENT": "1",
            },
            "PyAnnote raw, min=1 max=8, без resegment",
        ),
        (
            "pyannote_merge_min1",
            {
                "PRESET_ID": "pyannote_merge_min1",
                "SKIP_RESEGMENT": "1",
                "MERGE_CENTROIDS": "1",
                "MERGE_CENTROID_THRESHOLD": "0.82",
            },
            "PyAnnote raw + merge centroids (0.82)",
        ),
        (
            "pyannote_merge_reseg_min1",
            {
                "PRESET_ID": "pyannote_merge_reseg_min1",
                "MERGE_CENTROIDS": "1",
                "MERGE_CENTROID_THRESHOLD": "0.82",
                "RESEG_SIM_MARGIN": "0.08",
                "RESEG_HOLD_SEC": "0.9",
            },
            "PyAnnote merge + resegment (margin 0.08)",
        ),
    ]

    ran: list[tuple[str, Path, str]] = []
    for name, env, label in variants:
        path = docker_diar(name, env)
        ran.append((name, path, label))

    sf = run_sortformer()
    if sf:
        ran.append(("sortformer", sf, "Sortformer v2.1 GGUF"))

    manifest = write_manifest(ran)
    build_html(manifest)
    print_timelines([(n, p) for n, p, _ in ran])
    print(f"\nmanifest: {manifest}")
    print(f"html: data/projects/1/artifacts/{ARTIFACT}/reports/diar_two_voice_4min.html")
    print(f"url: http://127.0.0.1:1111/artifacts/{ARTIFACT}/report-diar-two-voice")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
