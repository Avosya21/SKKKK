"""06_transcript — реплики из покрашенных слов. Без LLM и без переписывания.

Стадия только группирует уже готовые слова: ни одно слово не добавляется, не
удаляется и не переписывается. Это принципиально — на этой границе прежний
конвейер терял связь между текстом и звуком.

Спорные и бесхозные слова остаются в тексте, но помечены. Выкидывать их молча
нельзя: пропавшая реплика хуже помеченной, а по пометке видно, где смотреть.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..paths import LabRun
from ..run import StageContext, execute_stage, read_run
from .s02_asr import words_path
from .s03_diar import diar_path
from .s04_paint import ASSIGNED, CONTESTED, UNASSIGNED, paint_path

STAGE = "06_transcript"

TRANSCRIPT_JSON = "transcript.json"
TRANSCRIPT_TXT = "transcript.txt"
TIMECODES_TXT = "transcript_timecodes.txt"

UNKNOWN_SPEAKER = "unknown"


def transcript_path(run: LabRun) -> Path:
    return run.stage_dir(STAGE) / TRANSCRIPT_JSON


def _timecode(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def build_replicas(
    words: list[dict[str, Any]],
    *,
    max_gap: float,
    keep_unassigned: bool,
) -> list[dict[str, Any]]:
    """Слова → реплики. Смена спикера или пауза длиннее ``max_gap`` рвут реплику."""
    replicas: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for word in words:
        verdict = word.get("verdict")
        if verdict == UNASSIGNED and not keep_unassigned:
            continue
        speaker = (
            UNKNOWN_SPEAKER
            if word.get("speaker_index") is None
            else f"speaker_{int(word['speaker_index'])}"
        )
        start, end = float(word["start"]), float(word["end"])
        if (
            current is None
            or current["speaker"] != speaker
            or start - current["end"] > max_gap
        ):
            current = {
                "index": len(replicas),
                "speaker": speaker,
                "start": start,
                "end": end,
                "word_from": word["i"],
                "word_to": word["i"],
                "words": [],
                "counts": {ASSIGNED: 0, CONTESTED: 0, UNASSIGNED: 0},
            }
            replicas.append(current)
        current["end"] = end
        current["word_to"] = word["i"]
        current["counts"][verdict] = current["counts"].get(verdict, 0) + 1
        current["words"].append(
            {
                "i": word["i"],
                "word": word["word"],
                "start": start,
                "end": end,
                "verdict": verdict,
                "confidence": word.get("confidence"),
            }
        )

    for replica in replicas:
        replica["text"] = " ".join(w["word"] for w in replica["words"])
        replica["duration"] = round(replica["end"] - replica["start"], 3)
        replica["start"] = round(replica["start"], 3)
        replica["end"] = round(replica["end"], 3)
        replica["flagged"] = replica["counts"][CONTESTED] + replica["counts"][UNASSIGNED]
    return replicas


def run(
    lab_run: LabRun,
    *,
    max_gap: float = 1.2,
    keep_unassigned: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    paint_file = paint_path(lab_run)
    params: dict[str, Any] = {
        "max_gap": max_gap,
        "keep_unassigned": keep_unassigned,
        "llm": False,
    }

    def work(ctx: StageContext) -> None:
        meta = read_run(lab_run)
        paint_doc = json.loads(paint_file.read_text(encoding="utf-8"))
        words_doc = json.loads(words_path(lab_run).read_text(encoding="utf-8"))
        diar_doc = json.loads(diar_path(lab_run).read_text(encoding="utf-8"))

        replicas = build_replicas(
            paint_doc["words"], max_gap=max_gap, keep_unassigned=keep_unassigned
        )
        if not replicas:
            raise RuntimeError("из покрашенных слов не собралось ни одной реплики")

        provenance = {
            "run_id": lab_run.run_id,
            "artifact_id": meta.get("artifact_id"),
            "project_id": meta.get("project_id"),
            "source_sha256": (meta.get("source") or {}).get("sha256"),
            "asr_engine": words_doc.get("engine"),
            "diar_model": diar_doc.get("model"),
            "paint_thresholds": paint_doc.get("thresholds"),
            "binarization": diar_doc.get("binarization"),
        }
        ctx.write_json(
            TRANSCRIPT_JSON,
            {
                "stage": STAGE,
                "provenance": provenance,
                "counts": paint_doc.get("counts"),
                "replica_count": len(replicas),
                "speakers": sorted({r["speaker"] for r in replicas}),
                "replicas": replicas,
            },
        )

        plain = "\n".join(f"{r['speaker']}: {r['text']}" for r in replicas)
        timed = "\n".join(
            f"[{_timecode(r['start'])} - {_timecode(r['end'])}] {r['speaker']}: {r['text']}"
            for r in replicas
        )
        for name, text in ((TRANSCRIPT_TXT, plain), (TIMECODES_TXT, timed)):
            path = ctx.out(name)
            path.write_text(text + "\n", encoding="utf-8")
            ctx.add_output(path)

        flagged = sum(1 for r in replicas if r["flagged"])
        ctx.metrics = {
            "replica_count": len(replicas),
            "speakers": len({r["speaker"] for r in replicas}),
            "flagged_replicas": flagged,
            "chars": len(plain),
            "unknown_replicas": sum(1 for r in replicas if r["speaker"] == UNKNOWN_SPEAKER),
        }
        ctx.log(f"{len(replicas)} реплик, помечено {flagged}")

    return execute_stage(
        lab_run,
        STAGE,
        inputs={
            "paint": paint_file,
            "words": words_path(lab_run),
            "diar": diar_path(lab_run),
        },
        params=params,
        work=work,
        force=force,
    )
