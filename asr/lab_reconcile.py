"""Этап 3b — построчная сверка снимка 1+2 с прогоном GigaAM.

Читает ``04_paint/paint.json`` (этап 1), ``sense/sense.json`` (этап 2) и
``giga/giga.json`` (этап 3a). Пишет только ``reconcile/``: приговоры по спорным
кускам, сопоставление спикеров и расхождения текста.

Ни один файл этапов 1–3a не меняется.

    venv\\Scripts\\python.exe asr\\lab_reconcile.py --project-id 1 --artifact-id art_... --run-id 20260826T174104Z_48a796
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

from asr.lab.giga import GIGA_DIR  # noqa: E402
from asr.lab.paths import LabRun  # noqa: E402
from asr.lab.reconcile import (  # noqa: E402
    CLOSED,
    CONFIRMED,
    CONFLICT,
    counts,
    judge_spans,
    map_speakers,
    text_windows,
)
from asr.lab.sense import apply_overlay  # noqa: E402
from asr.lab.stages.s04_paint import paint_path  # noqa: E402

RECONCILE_DIR = "reconcile"


def main() -> int:
    parser = argparse.ArgumentParser(description="Этап 3b: сверка снимка 1+2 с GigaAM")
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--edge-margin", type=float, default=0.5)
    parser.add_argument("--text-similar", type=float, default=0.93)
    args = parser.parse_args()

    data_dir = Path(os.environ.get("DATA_DIR") or ROOT / "data")
    artifact_root = data_dir / "projects" / str(args.project_id) / "artifacts" / args.artifact_id
    run = LabRun(artifact_root, args.run_id)

    paint_file = paint_path(run)
    giga_file = run.resolve(f"{GIGA_DIR}/giga.json")
    for path in (paint_file, giga_file):
        if not path.is_file():
            print(f"нет {path}", file=sys.stderr)
            return 3

    paint = json.loads(paint_file.read_text(encoding="utf-8"))
    sense_file = run.resolve("sense/sense.json")
    sense = json.loads(sense_file.read_text(encoding="utf-8")) if sense_file.is_file() else {}
    words = apply_overlay(paint["words"], sense.get("decisions") or [])
    giga = json.loads(giga_file.read_text(encoding="utf-8"))
    turns = giga["turns"]

    mapping = map_speakers(words, turns)
    spans = judge_spans(words, turns, mapping, edge_margin=args.edge_margin)
    windows = text_windows(words, turns, similar=args.text_similar)

    doc = {
        "stage": "reconcile",
        "overlay": True,
        "does_not_replace": [
            "02_asr/words.json",
            "04_paint/paint.json",
            "06_transcript/transcript.json",
            "sense/sense.json",
            "giga/giga.json",
        ],
        "meta": {
            "run_id": run.run_id,
            "artifact_id": args.artifact_id,
            "edge_margin": args.edge_margin,
            "text_similar": args.text_similar,
            "giga_task_id": (giga.get("meta") or {}).get("task_id"),
            "lab_words": len(words),
            "giga_turns": len(turns),
        },
        "speaker_map": mapping,
        "span_counts": counts(spans, "verdict"),
        "spans": spans,
    }
    disagree = [row for row in windows if not row["agree"]]
    text_doc = {
        "stage": "reconcile_text",
        "meta": doc["meta"],
        "summary": {
            "windows": len(windows),
            "agree": len(windows) - len(disagree),
            "disagree": len(disagree),
            "mean_similarity": (
                round(sum(row["similarity"] for row in windows) / len(windows), 4)
                if windows
                else None
            ),
            "our_chars": sum(len(row["our_text"]) for row in windows),
            "giga_chars": sum(len(row["giga_text"]) for row in windows),
        },
        "windows": windows,
    }

    target = run.resolve(RECONCILE_DIR)
    target.mkdir(parents=True, exist_ok=True)
    (target / "speakers.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (target / "text.json").write_text(
        json.dumps(text_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    named = {
        key: row["speaker_index"]
        for key, row in mapping.items()
        if row.get("speaker_index") is not None
    }
    print(
        json.dumps(
            {
                "run_id": run.run_id,
                "speaker_map": named,
                "unmapped": sorted(set(mapping) - set(named)),
                "spans": doc["span_counts"],
                "closed": sum(1 for row in spans if row["verdict"] == CLOSED),
                "confirmed": sum(1 for row in spans if row["verdict"] == CONFIRMED),
                "conflict": sum(1 for row in spans if row["verdict"] == CONFLICT),
                "text": text_doc["summary"],
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
