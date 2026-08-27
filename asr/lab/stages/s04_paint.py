"""04_paint — покраска слов спикерами по матрице вероятностей.

Красим по сырым вероятностям, а не по бинарным сегментам: на границе реплик
сегмент обрывается жёстко, а вероятность показывает, что слово попало в спорную
зону. Спорное и бесхозное складываем в отдельные корзины и **не прячем** —
именно из молчаливой раздачи таких слов ближайшему спикеру и рождается
ощущение, что текст «подмешался».

Три исхода на слово:

* ``assigned``   — уверенный лидер с достаточным отрывом;
* ``contested``  — два спикера рядом, отрыв меньше порога;
* ``unassigned`` — по вероятностям в этом месте не говорит никто. Это либо
  придуманное на тишине слово, либо промах диаризации; решает человек.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from ..paths import LabRun
from ..run import StageContext, execute_stage
from ..sortformer import FRAME_DURATION
from .s02_asr import words_path
from .s03_diar import PROBS_CLEAN, diar_path

STAGE = "04_paint"

PAINT_JSON = "paint.json"

ASSIGNED = "assigned"
CONTESTED = "contested"
UNASSIGNED = "unassigned"


def paint_path(run: LabRun) -> Path:
    return run.stage_dir(STAGE) / PAINT_JSON


def probs_path(run: LabRun) -> Path:
    return run.stage_dir("03_diar") / PROBS_CLEAN


# 0.8 / 0.08 в двоичной плавающей точке даёт 9.999999999999998, и граница слова
# уезжает на кадр назад. Допуск меньше миллисекунды это чинит.
_FRAME_EPS = 1e-6


def word_frames(start: float, end: float, total_frames: int) -> tuple[int, int]:
    """Кадры, покрывающие слово. Слово короче кадра всё равно получает один кадр."""
    first = max(0, math.floor(start / FRAME_DURATION + _FRAME_EPS))
    last = min(total_frames, max(first + 1, math.ceil(end / FRAME_DURATION - _FRAME_EPS)))
    if first >= total_frames:
        first = max(0, total_frames - 1)
        last = total_frames
    return first, last


def classify(
    means: np.ndarray,
    *,
    min_prob: float,
    min_margin: float,
) -> tuple[str, int | None, float, float]:
    order = np.argsort(-means)
    top = int(order[0])
    best = float(means[top])
    second = float(means[order[1]]) if means.size > 1 else 0.0
    margin = round(best - second, 4)
    if best < min_prob:
        return UNASSIGNED, None, round(best, 4), margin
    if margin < min_margin:
        return CONTESTED, top, round(best, 4), margin
    return ASSIGNED, top, round(best, 4), margin


def run(
    lab_run: LabRun,
    *,
    min_prob: float = 0.35,
    min_margin: float = 0.15,
    force: bool = False,
) -> dict[str, Any]:
    words_file = words_path(lab_run)
    probs_file = probs_path(lab_run)
    diar_file = diar_path(lab_run)
    params: dict[str, Any] = {
        "min_prob": min_prob,
        "min_margin": min_margin,
        "source": "probabilities",
    }

    def work(ctx: StageContext) -> None:
        words_doc = json.loads(words_file.read_text(encoding="utf-8"))
        diar_doc = json.loads(diar_file.read_text(encoding="utf-8"))
        probabilities = np.load(probs_file)
        total_frames = int(probabilities.shape[0])
        if total_frames == 0:
            raise RuntimeError("матрица вероятностей пуста — диаризация не дала кадров")

        rows: list[dict[str, Any]] = []
        counters = {ASSIGNED: 0, CONTESTED: 0, UNASSIGNED: 0}
        for word in words_doc["words"]:
            first, last = word_frames(float(word["start"]), float(word["end"]), total_frames)
            window = probabilities[first:last]
            means = window.mean(axis=0) if window.size else np.zeros(probabilities.shape[1])
            verdict, speaker, best, margin = classify(
                means, min_prob=min_prob, min_margin=min_margin
            )
            counters[verdict] += 1
            rows.append(
                {
                    "i": word["i"],
                    "word": word["word"],
                    "start": word["start"],
                    "end": word["end"],
                    "verdict": verdict,
                    "speaker_index": speaker,
                    "speaker": None if speaker is None else f"speaker_{speaker}",
                    "confidence": best,
                    "margin": margin,
                    "probs": [round(float(v), 3) for v in means],
                    "frames": [first, last],
                }
            )

        total = len(rows)
        payload = {
            "run_id": lab_run.run_id,
            "stage": STAGE,
            "words_source": ctx.inputs["words"].as_dict(),
            "diar_source": diar_doc.get("audio"),
            "thresholds": {"min_prob": min_prob, "min_margin": min_margin},
            "counts": counters,
            "word_count": total,
            "words": rows,
        }
        ctx.write_json(PAINT_JSON, payload)

        ctx.metrics = {
            "word_count": total,
            **counters,
            "assigned_ratio": round(counters[ASSIGNED] / total, 3) if total else 0.0,
            "unassigned_sec": round(
                sum(r["end"] - r["start"] for r in rows if r["verdict"] == UNASSIGNED), 2
            ),
            "speakers_used": sorted(
                {r["speaker_index"] for r in rows if r["speaker_index"] is not None}
            ),
        }
        ctx.log(
            "покрашено {0}, спорных {1}, бесхозных {2} из {3}".format(
                counters[ASSIGNED], counters[CONTESTED], counters[UNASSIGNED], total
            )
        )

    return execute_stage(
        lab_run,
        STAGE,
        inputs={"words": words_file, "probs": probs_file, "diar": diar_file},
        params=params,
        work=work,
        force=force,
    )
