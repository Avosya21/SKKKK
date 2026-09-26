"""Sortformer v2.1 GGUF → sortformer_segments.json в OUT (и reports/ артефакта).

  python asr/dump_sortformer.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "asr"))
from paths import OUT, WORK  # noqa: E402

PS1 = ROOT / "experiments" / "nemo-diar" / "run_sortformer_gguf.ps1"


def _find_report(out_dir: Path, stem: str) -> Path | None:
    for tag in ("vulkan", "cuda", "cpu"):
        candidate = out_dir / f"sortformer_gguf_{tag}_{stem}_report.json"
        if candidate.is_file():
            return candidate
    matches = sorted(out_dir.glob(f"sortformer_gguf_*_{stem}_report.json"))
    return matches[-1] if matches else None


def main() -> int:
    audio = WORK / "input_16k.wav"
    if not audio.is_file():
        raise SystemExit(f"нет {audio}")
    if not PS1.is_file():
        raise SystemExit(f"нет скрипта Sortformer: {PS1}")

    run_dir = OUT / "sortformer_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PS1),
            "-Audio",
            str(audio),
            "-OutDir",
            str(run_dir),
        ],
        cwd=str(ROOT),
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"run_sortformer_gguf.ps1 failed: {proc.returncode}")

    report = _find_report(run_dir, audio.stem)
    if report is None:
        raise SystemExit(f"Sortformer report not found in {run_dir}")

    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / "sortformer_segments.json"
    shutil.copy2(report, dest)

    reports_env = (OUT.parent / "reports") if OUT.name == "asr" else None
    if reports_env is not None:
        reports_env.mkdir(parents=True, exist_ok=True)
        shutil.copy2(report, reports_env / "sortformer_segments.json")

    data = json.loads(dest.read_text(encoding="utf-8"))
    n = len(data.get("segments") or [])
    print("wrote", dest, "segments", n, "speakers", data.get("speaker_count"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
