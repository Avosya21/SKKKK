"""03_diar — Sortformer 4spk v2.1, сырые вероятности плюс своя бинаризация.

Матрица T x 4 сохраняется целиком: именно по ней потом видно, что в спорном
месте модель сомневалась (0.5/0.5), а не «уверенно ошиблась». Пороги хранятся
в параметрах стадии, поэтому перебинаризовать прогон можно без повторного
инференса — достаточно поменять пороги и перезапустить стадию.

Прогон по второму WAV (``--compare-source``) — отладочная сверка, не часть
нормального пути: денойза больше нет, сравнивать нечего.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from ..onnx_env import providers_for, runtime_info
from ..paths import LabRun
from ..run import StageContext, execute_stage, source_audio
from ..sortformer import (
    FRAME_DURATION,
    NUM_SPEAKERS,
    Binarization,
    SortformerOnnx,
    binarize,
    resolve_model,
)
from .s01_audio import clean_wav

STAGE = "03_diar"

DIAR_JSON = "diar.json"
PROBS_CLEAN = "probs_clean.npy"
PROBS_SOURCE = "probs_source.npy"
HEATMAP_JSON = "heatmap.json"
COMPARE_JSON = "compare.json"

HEATMAP_MAX_COLUMNS = 2400


def diar_path(run: LabRun) -> Path:
    return run.stage_dir(STAGE) / DIAR_JSON


def _speaker_stats(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stats: dict[int, dict[str, Any]] = {}
    for row in segments:
        entry = stats.setdefault(
            row["speaker_index"],
            {"speaker": row["speaker"], "speaker_index": row["speaker_index"], "segments": 0, "seconds": 0.0},
        )
        entry["segments"] += 1
        entry["seconds"] += row["end"] - row["start"]
    for entry in stats.values():
        entry["seconds"] = round(entry["seconds"], 2)
    return [stats[key] for key in sorted(stats)]


def _overlap_seconds(probabilities: np.ndarray, onset: float) -> float:
    active = (probabilities >= onset).sum(axis=1)
    return round(float((active >= 2).sum()) * FRAME_DURATION, 2)


def _heatmap(probabilities: np.ndarray) -> dict[str, Any]:
    """Ужать матрицу максимумом по окну: пики важнее среднего для глаза."""
    frames = probabilities.shape[0]
    if frames == 0:
        return {"pool": 1, "frame_sec": FRAME_DURATION, "columns": 0, "values": []}
    pool = max(1, -(-frames // HEATMAP_MAX_COLUMNS))
    usable = (frames // pool) * pool
    trimmed = probabilities[:usable].reshape(-1, pool, probabilities.shape[1]).max(axis=1)
    tail = probabilities[usable:]
    if tail.size:
        trimmed = np.concatenate([trimmed, tail.max(axis=0, keepdims=True)], axis=0)
    return {
        "pool": pool,
        "frame_sec": FRAME_DURATION,
        "column_sec": round(FRAME_DURATION * pool, 4),
        "columns": int(trimmed.shape[0]),
        "speakers": int(trimmed.shape[1]),
        "values": [[round(float(v), 2) for v in row] for row in trimmed],
    }


def run(
    lab_run: LabRun,
    *,
    device: str = "auto",
    onset: float = 0.64,
    offset: float = 0.74,
    pad_onset: float = 0.06,
    pad_offset: float = 0.0,
    min_duration_on: float = 0.10,
    min_duration_off: float = 0.15,
    compare_with_source: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    audio = clean_wav(lab_run)
    original = source_audio(lab_run)
    rules = Binarization(
        onset=onset,
        offset=offset,
        pad_onset=pad_onset,
        pad_offset=pad_offset,
        min_duration_on=min_duration_on,
        min_duration_off=min_duration_off,
    )
    params: dict[str, Any] = {
        "model": "diar_streaming_sortformer_4spk-v2.1",
        "device": device,
        "binarization": rules.as_dict(),
        "compare_with_source": bool(compare_with_source),
    }

    inputs = {"clean_16k": audio}
    if compare_with_source:
        inputs["source_wav"] = original

    def work(ctx: StageContext) -> None:
        providers = providers_for(device)
        model_path = resolve_model()
        ctx.log(f"Sortformer {model_path.name}, провайдеры {providers}")
        engine = SortformerOnnx(model_path, providers)

        ctx.log("инференс по рабочему WAV")
        clean = engine.diarize_file(audio)
        np.save(ctx.out(PROBS_CLEAN), clean.probabilities)
        ctx.add_output(ctx.out(PROBS_CLEAN))

        segments = binarize(clean.probabilities, rules=rules, audio_duration=clean.audio_duration)
        speakers = _speaker_stats(segments)
        payload = {
            "run_id": lab_run.run_id,
            "stage": STAGE,
            "audio": ctx.inputs["clean_16k"].as_dict(),
            "model": model_path.name,
            "frame_sec": FRAME_DURATION,
            "frames": clean.frames,
            "audio_duration_sec": round(clean.audio_duration, 2),
            "max_speakers": NUM_SPEAKERS,
            "binarization": rules.as_dict(),
            "speakers": speakers,
            "segment_count": len(segments),
            "overlap_sec": _overlap_seconds(clean.probabilities, onset),
            "segments": segments,
        }
        ctx.write_json(DIAR_JSON, payload)
        ctx.write_json(HEATMAP_JSON, _heatmap(clean.probabilities))

        ctx.metrics = {
            "frames": clean.frames,
            "segment_count": len(segments),
            "speakers_detected": len(speakers),
            "overlap_sec": payload["overlap_sec"],
            "speech_sec": round(sum(s["seconds"] for s in speakers), 2),
        }

        if compare_with_source:
            ctx.log("контрольный инференс по исходному WAV")
            dirty = engine.diarize_file(original)
            np.save(ctx.out(PROBS_SOURCE), dirty.probabilities)
            ctx.add_output(ctx.out(PROBS_SOURCE))
            dirty_segments = binarize(
                dirty.probabilities, rules=rules, audio_duration=dirty.audio_duration
            )
            dirty_speakers = _speaker_stats(dirty_segments)
            shared = min(clean.frames, dirty.frames)
            drift = (
                float(np.abs(clean.probabilities[:shared] - dirty.probabilities[:shared]).mean())
                if shared
                else 0.0
            )
            ctx.write_json(
                COMPARE_JSON,
                {
                    "clean": {
                        "frames": clean.frames,
                        "segment_count": len(segments),
                        "speakers": speakers,
                    },
                    "source": {
                        "frames": dirty.frames,
                        "segment_count": len(dirty_segments),
                        "speakers": dirty_speakers,
                    },
                    "mean_abs_prob_diff": round(drift, 4),
                    "speaker_count_changed": len(speakers) != len(dirty_speakers),
                },
            )
            ctx.metrics["source_speakers_detected"] = len(dirty_speakers)
            ctx.metrics["mean_abs_prob_diff"] = round(drift, 4)
            if len(speakers) != len(dirty_speakers):
                ctx.log(
                    f"внимание: спикеров на рабочем WAV {len(speakers)}, на исходном {len(dirty_speakers)}"
                )

        ctx.tool = {"name": "sortformer-onnx", "providers": engine.providers, **runtime_info()}
        ctx.log(f"{len(segments)} сегментов, спикеров {len(speakers)}")

    return execute_stage(
        lab_run,
        STAGE,
        inputs=inputs,
        params=params,
        work=work,
        force=force,
    )
