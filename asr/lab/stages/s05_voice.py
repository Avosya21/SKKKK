"""05_voice — слепки голосов WeSpeaker по покрашенным словам.

Клипы берутся не из сегментов диаризации, а из **прогонов слов**: подряд идущие
слова одного спикера. Так в слепок гарантированно попадает произнесённый текст,
а не пауза, которую диаризация посчитала речью.

Эмбеддинги считает контейнер ``primaryanalitics-gigaam`` — тот же WeSpeaker, которым
набита боевая база ``speaker_profiles``. Считать их другой моделью нельзя:
косинусы окажутся несопоставимы с уже сохранёнными профилями.

Контракт контейнера использует фиксированные ``data/gigaam/embed_job.json`` и
``embed_result.json``. Изолировать их нельзя, поэтому лаборатория их
**проверяет**: имя WAV содержит run_id, ответ обязан содержать ровно наши ключи,
а сам ответ сразу копируется внутрь прогона. Чужой результат приводит к падению
стадии, а не к тихой подстановке.

В боевую базу стадия не пишет ничего: только читает профили для подсказок.
Этап 4 (``asr/lab_voice.py``) живёт в ``voice/`` и не перезапускает эту стадию.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..paths import ROOT, LabRun
from ..run import StageContext, execute_stage, read_run
from ..voice import (
    EMBED_RAW_JSON,
    GIGAAM_CONTAINER,
    VOICE_JSON,
    clip_embeddings,
    clips_from_runs,
    centroids_from_clips,
    cross_speaker_cosine,
    docker_embed,
    load_profiles,
    profile_candidates,
    stage_embed_audio,
    word_runs,
)
from .s01_audio import clean_wav
from .s04_paint import paint_path

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "asr") not in sys.path:
    sys.path.insert(0, str(ROOT / "asr"))

STAGE = "05_voice"

# Реэкспорт для тестов, которые импортируют клипы из этой стадии.
__all__ = ["STAGE", "clips_from_runs", "run", "voice_path", "word_runs"]


def voice_path(run: LabRun) -> Path:
    return run.stage_dir(STAGE) / VOICE_JSON


def run(
    lab_run: LabRun,
    *,
    max_gap: float = 0.6,
    edge_trim: float = 0.25,
    min_duration: float = 2.0,
    max_duration: float = 12.0,
    per_speaker: int = 3,
    top_candidates: int = 3,
    force: bool = False,
) -> dict[str, Any]:
    paint_file = paint_path(lab_run)
    audio = clean_wav(lab_run)
    params: dict[str, Any] = {
        "max_gap": max_gap,
        "edge_trim": edge_trim,
        "min_duration": min_duration,
        "max_duration": max_duration,
        "per_speaker": per_speaker,
        "embedder": "wespeaker (primaryanalitics-gigaam)",
    }

    def work(ctx: StageContext) -> None:
        meta = read_run(lab_run)
        paint_doc = json.loads(paint_file.read_text(encoding="utf-8"))
        runs = word_runs(paint_doc["words"], max_gap=max_gap)
        clips = clips_from_runs(
            runs,
            edge_trim=edge_trim,
            min_duration=min_duration,
            max_duration=max_duration,
            per_speaker=per_speaker,
        )
        if not clips:
            raise RuntimeError(
                "не нашлось ни одного прогона слов длиннее "
                f"{min_duration} с — нечего превращать в слепок голоса"
            )
        ctx.log(f"{len(runs)} прогонов слов, {len(clips)} клипов на эмбеддинг")

        staged = stage_embed_audio(lab_run, audio)
        payload = docker_embed(staged, clips)
        ctx.write_json(EMBED_RAW_JSON, payload)
        staged.unlink(missing_ok=True)

        embeddings = clip_embeddings(payload)
        if not embeddings:
            raise RuntimeError("контейнер вернул клипы без эмбеддингов")

        centroids = centroids_from_clips(clips, embeddings)
        cross = cross_speaker_cosine(centroids)
        profiles = load_profiles(int(meta.get("project_id") or 0))
        candidates = profile_candidates(centroids, profiles, top=top_candidates)

        ctx.write_json(
            VOICE_JSON,
            {
                "run_id": lab_run.run_id,
                "stage": STAGE,
                "artifact_id": meta.get("artifact_id"),
                "project_id": meta.get("project_id"),
                "written_to_db": False,
                "clip_count": len(clips),
                "run_count": len(runs),
                "embedding_dim": len(next(iter(embeddings.values()))),
                "clips": [
                    {"key": key, **row, "embedded": key in embeddings}
                    for key, row in clips.items()
                ],
                "cross_speaker_cosine": cross,
                "profile_candidates": candidates,
                "profiles_compared": len(profiles),
            },
        )

        ctx.tool = {"name": "wespeaker", "container": GIGAAM_CONTAINER}
        ctx.metrics = {
            "runs": len(runs),
            "clips": len(clips),
            "embedded": len(embeddings),
            "speakers": len(centroids),
            "profiles_compared": len(profiles),
            "max_cross_cosine": max((row["cosine"] for row in cross), default=None),
        }
        ctx.log(f"эмбеддингов {len(embeddings)}, спикеров {len(centroids)}")

    return execute_stage(
        lab_run,
        STAGE,
        inputs={"paint": paint_file, "clean_16k": audio},
        params=params,
        work=work,
        force=force,
    )
