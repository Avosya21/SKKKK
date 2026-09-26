"""01_audio — рабочая копия исходника в 16 кГц внутри прогона.

Денойза нет: DeepFilterNet срезал слабые согласные, а распознавание на сыром
файле оказалось приемлемым. Стадия всё равно копирует WAV в каталог прогона —
последующие шаги читают только этот файл, а не глобальный ``input_16k.wav``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..audio import SAMPLE_RATE_16K, duration_sec, noise_floor_db, read_mono, resample_wav, rms_db
from ..paths import LabRun
from ..run import StageContext, execute_stage, source_audio

STAGE = "01_audio"

CLEAN_16K = "clean_16k.wav"


def clean_wav(run: LabRun) -> Path:
    """Рабочий 16 кГц WAV прогона. Имя историческое, очистки в нём нет."""
    return run.stage_dir(STAGE) / CLEAN_16K


def run(lab_run: LabRun, *, force: bool = False) -> dict[str, Any]:
    src = source_audio(lab_run)
    params: dict[str, Any] = {
        "mode": "copy_16k",
        "output_rate_hz": SAMPLE_RATE_16K,
    }

    def work(ctx: StageContext) -> None:
        dst16 = ctx.out(CLEAN_16K)
        ctx.log("рабочая копия исходника 16 кГц, без денойза")
        resample_wav(src, dst16, SAMPLE_RATE_16K)
        ctx.add_output(dst16)

        samples, sample_rate = read_mono(dst16)
        ctx.tool = {"name": "ffmpeg", "mode": "copy_16k"}
        ctx.metrics = {
            "duration_sec": round(duration_sec(dst16), 2),
            "rms_db": rms_db(samples),
            "noise_floor_db": noise_floor_db(samples, sample_rate),
        }
        ctx.log(
            "длительность {0:.1f} с, шумовая полка {1} дБ".format(
                ctx.metrics["duration_sec"], ctx.metrics["noise_floor_db"]
            )
        )

    return execute_stage(
        lab_run,
        STAGE,
        inputs={"source_wav": src},
        params=params,
        work=work,
        force=force,
    )
