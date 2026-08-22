"""Снять сырые интервалы PyAnnote из контейнера skkkk-gigaam.

  python asr/dump_pyannote.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "asr"))
from paths import OUT  # noqa: E402
SRC = ROOT / "data" / "gigaam" / "pyannote_segments.json"
GIGAAM = "http://127.0.0.1:18102"


def main() -> int:
    subprocess.run(["docker", "start", "skkkk-gigaam"], check=False)
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{GIGAAM}/health", timeout=5).read()
            break
        except Exception:
            time.sleep(3)
    else:
        raise SystemExit("skkkk-gigaam не поднялся")
    proc = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            "DIARIZATION_MIN_SPEAKERS=4",
            "-e",
            "DIARIZATION_MAX_SPEAKERS=8",
            "-e",
            "CLUSTER_THRESHOLD=0.52",
            "-e",
            "CLUSTER_MIN_SIZE=6",
            "-e",
            "CLUSTER_METHOD=centroid",
            "skkkk-gigaam",
            "python",
            "/data/dump_pyannote.py",
        ],
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"dump_pyannote inside container failed: {proc.returncode}")
    if not SRC.is_file():
        raise SystemExit(f"нет {SRC}")
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SRC, OUT / "pyannote_segments.json")
    data = json.loads(SRC.read_text(encoding="utf-8"))
    print("copied", OUT / "pyannote_segments.json", "speakers", data.get("speakers"), "n", len(data.get("segments") or []))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
