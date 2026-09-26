"""Подписать лабораторных спикеров именами с боя — без копирования нарезки.

Запуск из основного venv:

    venv\\Scripts\\python.exe asr\\lab_hint.py --project-id 1 --artifact-id art_... --run-id 20260826T174104Z_48a796
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.hint import HINT_DIR, map_lab_speakers_to_prod_names  # noqa: E402
from asr.lab.paths import LabRun  # noqa: E402
from asr.lab.sense import apply_overlay  # noqa: E402
from asr.lab.stages.s04_paint import paint_path  # noqa: E402

PROD_SPEAKERS = "asr/docker_gigaam_speakers.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Имена с боя как подписи, не как основа")
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    data_dir = Path(os.environ.get("DATA_DIR") or ROOT / "data")
    artifact_root = data_dir / "projects" / str(args.project_id) / "artifacts" / args.artifact_id
    prod_path = artifact_root / "asr" / "docker_gigaam_speakers.json"
    if not prod_path.is_file():
        print(f"нет {prod_path} — боевая стенограмма не подпись", file=sys.stderr)
        return 3

    run = LabRun(artifact_root, args.run_id)
    paint_file = paint_path(run)
    sense_file = run.resolve("sense/sense.json")
    if not paint_file.is_file():
        print(f"нет {paint_file}", file=sys.stderr)
        return 3

    paint = json.loads(paint_file.read_text(encoding="utf-8"))
    sense = json.loads(sense_file.read_text(encoding="utf-8")) if sense_file.is_file() else {}
    words = apply_overlay(paint["words"], sense.get("decisions") or [])
    prod = json.loads(prod_path.read_text(encoding="utf-8"))
    doc = map_lab_speakers_to_prod_names(words, prod.get("turns") or [])
    doc["meta"] = {
        "run_id": run.run_id,
        "artifact_id": args.artifact_id,
        "prod_path": PROD_SPEAKERS,
        "paint_unchanged": True,
        "sense_unchanged": True,
    }
    out = run.resolve(f"{HINT_DIR}/names.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    named = {
        key: row["display_name"]
        for key, row in (doc.get("speakers") or {}).items()
        if row.get("display_name")
    }
    print(
        json.dumps(
            {
                "run_id": run.run_id,
                "path": out.as_posix(),
                "named": named,
                "leftover_open": len(doc.get("leftover") or []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    os.chdir(ROOT)
    raise SystemExit(main())
