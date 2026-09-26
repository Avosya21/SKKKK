"""Этап 3в — вычитка текста снимка 1+2 по прогону GigaAM.

Расхождения находит ``asr/lab/proof.py``, выбор по каждому делает Cursor API.
Модель выбирает только между двумя услышанными вариантами; своего текста не
пишет. Спикеры и времена берутся из снимка 1+2 и не меняются.

Пишем только в ``proof/``. Этапы 1, 2 и 3a остаются как есть.

    venv\\Scripts\\python.exe asr\\lab_proof.py --project-id 1 --artifact-id art_... --run-id 20260826T174104Z_48a796
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
from asr.lab.proof import (  # noqa: E402
    BATCH_DIFFS,
    PICK_GIGA,
    apply_picks,
    batched,
    collect_diffs,
    final_replicas,
    parse_picks,
)
from asr.lab.sense import apply_overlay  # noqa: E402
from asr.lab.stages.s04_paint import paint_path  # noqa: E402

PROMPT = ROOT / "docs" / "prompts" / "lab_proof_text.md"
PROOF_DIR = "proof"
LLM_FIELDS = ("id", "op", "left", "lab", "giga", "right")


def _write(run: LabRun, relative: str, payload: object) -> Path:
    path = run.resolve(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        path.write_text(str(payload), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Этап 3в: вычитка текста по GigaAM")
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dry-run", action="store_true", help="только собрать расхождения")
    parser.add_argument("--limit", type=int, help="взять первые N расхождений (проба)")
    args = parser.parse_args()

    data_dir = Path(os.environ.get("DATA_DIR") or ROOT / "data")
    root = data_dir / "projects" / str(args.project_id) / "artifacts" / args.artifact_id
    run = LabRun(root, args.run_id)

    paint_file = paint_path(run)
    giga_file = run.resolve("giga/giga.json")
    for path in (paint_file, giga_file):
        if not path.is_file():
            print(f"нет {path}", file=sys.stderr)
            return 3

    paint = json.loads(paint_file.read_text(encoding="utf-8"))
    sense_file = run.resolve("sense/sense.json")
    sense = json.loads(sense_file.read_text(encoding="utf-8")) if sense_file.is_file() else {}
    words = apply_overlay(paint["words"], sense.get("decisions") or [])
    turns = json.loads(giga_file.read_text(encoding="utf-8"))["turns"]

    diffs = collect_diffs(words, turns)
    asked = [row for row in diffs if not row["cosmetic"]]
    if args.limit:
        asked = asked[: args.limit]

    meta = {
        "run_id": run.run_id,
        "artifact_id": args.artifact_id,
        "overlay": True,
        "frozen_pipeline": "asr/lab/FREEZE.md",
        "alignment": "одно выравнивание на всю запись, без окон",
        "lab_words": len(words),
        "giga_turns": len(turns),
        "diff_count": len(diffs),
        "cosmetic": sum(1 for row in diffs if row["cosmetic"]),
        "asked": len(asked),
        "ops": {
            op: sum(1 for row in asked if row["op"] == op)
            for op in sorted({row["op"] for row in asked})
        },
        "dry_run": bool(args.dry_run),
    }
    _write(run, f"{PROOF_DIR}/diffs.json", {"meta": meta, "diffs": diffs})
    if args.dry_run:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return 0

    from app.app.services.llm import chat_from_env_no_think, strip_json_fence

    system = PROMPT.read_text(encoding="utf-8")
    allowed = {row["id"] for row in asked}
    picks: dict[str, str] = {}
    batches_log: list[dict] = []
    for number, batch in enumerate(batched(asked, BATCH_DIFFS), start=1):
        payload = [{key: row[key] for key in LLM_FIELDS} for row in batch]
        user = json.dumps({"diffs": payload}, ensure_ascii=False)
        text, _usage = chat_from_env_no_think(system, user, temperature=0.0, max_tokens=2048)
        try:
            raw = json.loads(strip_json_fence(text) or "{}")
        except json.JSONDecodeError:
            raw = {}
        parsed = parse_picks(raw, allowed)
        picks.update(parsed)
        batches_log.append({"batch": number, "diffs": len(batch), "picks": len(parsed)})
        print(
            f"[proof] партия {number}: {len(parsed)}/{len(batch)} решений",
            flush=True,
        )

    for row in diffs:
        row["pick"] = picks.get(row["id"])

    final_words = apply_picks(words, diffs)
    replicas = final_replicas(final_words)
    taken = sum(1 for row in diffs if row["pick"] == PICK_GIGA)

    doc = {
        "stage": "proof",
        "overlay": True,
        "does_not_replace": [
            "02_asr/words.json",
            "04_paint/paint.json",
            "06_transcript/transcript.json",
            "sense/sense.json",
            "giga/giga.json",
            "reconcile/text.json",
        ],
        "meta": meta,
        "batches": batches_log,
        "summary": {
            "diffs": len(diffs),
            "asked": len(asked),
            "answered": len(picks),
            "taken_giga": taken,
            "kept_lab": len(asked) - taken,
            "changed_words": sum(1 for word in final_words if word.get("proof")),
        },
        "diffs": diffs,
    }
    _write(run, f"{PROOF_DIR}/proof.json", doc)
    _write(
        run,
        f"{PROOF_DIR}/transcript.json",
        {
            "stage": "proof_transcript",
            "replica_count": len(replicas),
            "speakers": sorted({row["speaker"] for row in replicas}),
            "replicas": replicas,
        },
    )
    _write(
        run,
        f"{PROOF_DIR}/transcript.txt",
        "".join(
            f"[{int(row['start']) // 60:02d}:{int(row['start']) % 60:02d}] "
            f"{row['speaker']}: {row['text']}\n"
            for row in replicas
            if row["text"]
        ),
    )
    print(json.dumps({"run_id": run.run_id, **doc["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    os.chdir(ROOT)
    raise SystemExit(main())
