"""Оркестратор лабораторных стадий.

Порядок фиксирован, пропуски запрещены: нельзя покрасить слова, не распознав их.
Если нужной предыдущей стадии в прогоне нет, следующая падает на ``require_input``
с именем недостающего файла — это и есть замена прежним тихим fallback'ам.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import STAGES, LabRun, new_run_id
from .run import init_run, run_status
from .stages import s01_audio, s02_asr, s03_diar, s04_paint, s05_voice, s06_transcript, s07_verify

status = run_status


@dataclass
class LabOptions:
    """Параметры прогона. Всё, что влияет на результат, попадает в stage.json."""

    device: str = "auto"
    asr_model: str = s02_asr.DEFAULT_MODEL
    quantization: str | None = None
    onset: float = 0.64
    offset: float = 0.74
    compare_with_source: bool = False
    min_prob: float = 0.35
    min_margin: float = 0.15
    voice: bool = True
    per_speaker_clips: int = 3
    replica_gap: float = 1.2
    verify: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


def stage_runner(stage: str, run: LabRun, options: LabOptions, *, force: bool) -> dict[str, Any]:
    if stage == "01_audio":
        return s01_audio.run(run, force=force)
    if stage == "02_asr":
        return s02_asr.run(
            run,
            model_name=options.asr_model,
            device=options.device,
            quantization=options.quantization,
            force=force,
        )
    if stage == "03_diar":
        return s03_diar.run(
            run,
            device=options.device,
            onset=options.onset,
            offset=options.offset,
            compare_with_source=options.compare_with_source,
            force=force,
        )
    if stage == "04_paint":
        return s04_paint.run(
            run, min_prob=options.min_prob, min_margin=options.min_margin, force=force
        )
    if stage == "05_voice":
        return s05_voice.run(run, per_speaker=options.per_speaker_clips, force=force)
    if stage == "06_transcript":
        return s06_transcript.run(run, max_gap=options.replica_gap, force=force)
    if stage == "07_verify":
        return s07_verify.run(
            run, device=options.device, model_name=options.asr_model, force=force
        )
    raise ValueError(f"неизвестная стадия {stage!r}")


def planned_stages(options: LabOptions, *, only: str | None, start_from: str | None) -> list[str]:
    stages = list(STAGES)
    if not options.voice:
        stages.remove("05_voice")
    if not options.verify:
        stages.remove("07_verify")
    if only:
        if only not in STAGES:
            raise ValueError(f"неизвестная стадия {only!r}")
        return [only]
    if start_from:
        if start_from not in STAGES:
            raise ValueError(f"неизвестная стадия {start_from!r}")
        stages = [s for s in stages if STAGES.index(s) >= STAGES.index(start_from)]
    return stages


def create_run(
    artifact_root: Path,
    *,
    artifact_id: str,
    project_id: int,
    source_audio: Path,
    run_id: str | None = None,
) -> LabRun:
    run = LabRun(artifact_root, run_id or new_run_id())
    init_run(run, artifact_id=artifact_id, project_id=project_id, source_audio=source_audio)
    return run


def execute(
    run: LabRun,
    options: LabOptions,
    *,
    only: str | None = None,
    start_from: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Прогнать стадии по порядку. Первая же поломка останавливает конвейер."""
    stages = planned_stages(options, only=only, start_from=start_from)
    results: dict[str, Any] = {}
    for stage in stages:
        record = stage_runner(stage, run, options, force=force)
        results[stage] = {
            "status": record.get("status"),
            "skipped": bool(record.get("skipped")),
            "elapsed_sec": record.get("elapsed_sec"),
            "metrics": record.get("metrics"),
        }
    return {"run_id": run.run_id, "stages": results}
