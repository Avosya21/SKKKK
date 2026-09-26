"""Оверлей Cursor API: разнести спорные слова по смыслу, не трогая снимок 01–07.

Запуск из основного venv (там cursor-sdk), не из venvs\\lab:

    venv\\Scripts\\python.exe asr\\lab_sense.py --project-id 1 --artifact-id art_... --run-id 20260826T174104Z_48a796
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

from asr.lab.paths import LabRun  # noqa: E402
from asr.lab.sense import (  # noqa: E402
    BATCH_SPANS,
    apply_overlay,
    batched,
    disputed_spans,
    parse_decisions,
    span_payload,
)
from asr.lab.stages.s04_paint import paint_path  # noqa: E402
from asr.lab.stages.s06_transcript import build_replicas  # noqa: E402

PROMPT = ROOT / "docs" / "prompts" / "lab_sense_speakers.md"
SENSE_DIR = "sense"


def artifact_paths(project_id: int, artifact_id: str) -> tuple[Path, Path]:
    data_dir = Path(os.environ.get("DATA_DIR") or ROOT / "data")
    root = data_dir / "projects" / str(project_id) / "artifacts" / artifact_id
    audio = root / "audio" / "input_16k.wav"
    return root, audio


def _write(run: LabRun, relative: str, payload: object) -> Path:
    path = run.resolve(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        path.write_text(str(payload), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Смысловая разноска спорных слов лаборатории")
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dry-run", action="store_true", help="только собрать куски, без Cursor")
    args = parser.parse_args()

    root, _audio = artifact_paths(args.project_id, args.artifact_id)
    run = LabRun(root, args.run_id)
    paint_file = paint_path(run)
    if not paint_file.is_file():
        print(f"нет {paint_file} — сначала 04_paint", file=sys.stderr)
        return 3

    paint = json.loads(paint_file.read_text(encoding="utf-8"))
    words = paint["words"]
    spans = disputed_spans(words)
    payloads = [span_payload(words, span) for span in spans]
    allowed = {int(w["speaker_index"]) for w in words if w.get("speaker_index") is not None}

    meta = {
        "run_id": run.run_id,
        "overlay": True,
        "frozen_pipeline": "asr/lab/FREEZE.md",
        "paint_sha256_hint": str(paint.get("words_source")),
        "span_count": len(payloads),
        "disputed_words": sum(len(span) for span in spans),
        "dry_run": bool(args.dry_run),
    }
    _write(run, f"{SENSE_DIR}/spans.json", {"spans": payloads, "meta": meta})

    if args.dry_run:
        print(json.dumps({"spans": len(payloads), "words": meta["disputed_words"]}, ensure_ascii=False))
        return 0

    from app.app.services.llm import chat_from_env_no_think, strip_json_fence

    system = PROMPT.read_text(encoding="utf-8")
    decisions: list[dict] = []
    batches_log: list[dict] = []
    for batch in batched(payloads, BATCH_SPANS):
        user = json.dumps({"spans": batch, "allowed_speakers": sorted(allowed)}, ensure_ascii=False)
        text, _usage = chat_from_env_no_think(system, user, temperature=0.1, max_tokens=2048)
        try:
            parsed_raw = json.loads(strip_json_fence(text) or "{}")
        except json.JSONDecodeError:
            parsed_raw = {}
        parsed = parse_decisions(parsed_raw, allowed=allowed)
        decisions.extend(parsed)
        batches_log.append({"spans": len(batch), "decisions": len(parsed), "raw_chars": len(text)})

    painted = apply_overlay(words, decisions)
    replicas = build_replicas(painted, max_gap=1.2, keep_unassigned=True)
    sense_doc = {
        "stage": "sense",
        "overlay": True,
        "does_not_replace": ["02_asr/words.json", "04_paint/paint.json", "06_transcript/transcript.json"],
        "meta": meta,
        "batches": batches_log,
        "decisions": decisions,
        "assigned_words": sum(1 for word in painted if word.get("sense")),
    }
    _write(run, f"{SENSE_DIR}/sense.json", sense_doc)
    _write(
        run,
        f"{SENSE_DIR}/transcript.json",
        {
            "stage": "sense_transcript",
            "replica_count": len(replicas),
            "speakers": sorted({r["speaker"] for r in replicas}),
            "replicas": replicas,
        },
    )
    print(json.dumps(
        {
            "run_id": run.run_id,
            "spans": len(payloads),
            "decisions": len(decisions),
            "assigned_words": sense_doc["assigned_words"],
            "replicas": len(replicas),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    os.chdir(ROOT)
    raise SystemExit(main())
