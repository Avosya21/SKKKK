"""07_verify — независимая проверка: не выдумал ли ASR слова на тишине.

Второй проход тем же Parakeet, но через Silero VAD: модель видит только куски,
которые VAD считает речью. Это даёт сразу две вещи:

* границы речи — по ним ловятся слова первого прохода, целиком попавшие в
  тишину (классический источник «непонятных придуманных фраз»);
* независимую расшифровку — расхождение двух проходов измеримо и видно в
  отчёте, а не проявляется потом как «текст поплыл».

Стадия ничего не исправляет. Она выносит вердикт ``ok``/``warn``/``fail`` и
показывает конкретные слова. Правки решает человек: постфильтр, который молча
режет «подозрительное», уже был и делал только хуже.
"""

from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ..audio import duration_sec
from ..onnx_env import model_cache, providers_for, runtime_info
from ..paths import LabRun
from ..run import StageContext, execute_stage
from .s01_audio import clean_wav
from .s02_asr import DEFAULT_MODEL, words_path

STAGE = "07_verify"

VERIFY_JSON = "verify.json"

OK = "ok"
WARN = "warn"
FAIL = "fail"


def verify_path(run: LabRun) -> Path:
    return run.stage_dir(STAGE) / VERIFY_JSON


def _normalize(text: str) -> list[str]:
    return [
        piece
        for piece in "".join(c.lower() if c.isalnum() or c.isspace() else " " for c in text).split()
        if piece
    ]


def covered_ratio(start: float, end: float, spans: list[tuple[float, float]]) -> float:
    """Какая доля слова попадает в речь по VAD."""
    span = max(end - start, 1e-3)
    covered = 0.0
    for begin, stop in spans:
        overlap = min(end, stop) - max(start, begin)
        if overlap > 0:
            covered += overlap
    return min(1.0, covered / span)


def run(
    lab_run: LabRun,
    *,
    device: str = "auto",
    model_name: str = DEFAULT_MODEL,
    vad_threshold: float = 0.5,
    min_silence_ms: float = 300.0,
    speech_pad_ms: float = 120.0,
    min_coverage: float = 0.10,
    max_orphan_ratio: float = 0.02,
    force: bool = False,
) -> dict[str, Any]:
    audio = clean_wav(lab_run)
    words_file = words_path(lab_run)
    params: dict[str, Any] = {
        "model": model_name,
        "device": device,
        "vad": {
            "name": "silero",
            "threshold": vad_threshold,
            "min_silence_duration_ms": min_silence_ms,
            "speech_pad_ms": speech_pad_ms,
            "max_speech_duration_s": 80.0,
        },
        "min_coverage": min_coverage,
        "max_orphan_ratio": max_orphan_ratio,
    }

    def work(ctx: StageContext) -> None:
        import onnx_asr

        providers = providers_for(device)
        ctx.log(f"второй проход {model_name} через Silero VAD")
        vad = onnx_asr.load_vad("silero", str(model_cache("asr", "silero-vad")), providers=providers)
        model = (
            onnx_asr.load_model(model_name, str(model_cache("asr", model_name)), providers=providers)
            .with_vad(
                vad,
                threshold=vad_threshold,
                min_silence_duration_ms=min_silence_ms,
                speech_pad_ms=speech_pad_ms,
                max_speech_duration_s=80.0,
            )
            .with_timestamps()
        )

        spans: list[tuple[float, float]] = []
        pass_two_text: list[str] = []
        for segment in model.recognize(str(audio)):
            spans.append((float(segment.start), float(segment.end)))
            if segment.text:
                pass_two_text.append(segment.text)
        if not spans:
            raise RuntimeError("Silero VAD не нашёл ни одного речевого участка")

        words_doc = json.loads(words_file.read_text(encoding="utf-8"))
        orphans: list[dict[str, Any]] = []
        for word in words_doc["words"]:
            coverage = covered_ratio(float(word["start"]), float(word["end"]), spans)
            if coverage <= min_coverage:
                orphans.append(
                    {
                        "i": word["i"],
                        "word": word["word"],
                        "start": word["start"],
                        "end": word["end"],
                        "vad_coverage": round(coverage, 3),
                    }
                )

        total_words = len(words_doc["words"])
        orphan_ratio = round(len(orphans) / total_words, 4) if total_words else 0.0
        similarity = SequenceMatcher(
            None, _normalize(words_doc.get("words_text", "")), _normalize(" ".join(pass_two_text))
        ).ratio()

        speech_sec = sum(stop - start for start, stop in spans)
        total_sec = duration_sec(audio)
        verdict = OK
        reasons: list[str] = []
        if orphans:
            verdict = WARN
            reasons.append(f"{len(orphans)} слов вне речи по VAD")
        if orphan_ratio > max_orphan_ratio:
            verdict = FAIL
            reasons.append(f"доля слов вне речи {orphan_ratio} выше порога {max_orphan_ratio}")
        if similarity < 0.80:
            verdict = FAIL if verdict == FAIL else WARN
            reasons.append(f"проходы расходятся, сходство {similarity:.2f}")

        ctx.write_json(
            VERIFY_JSON,
            {
                "run_id": lab_run.run_id,
                "stage": STAGE,
                "verdict": verdict,
                "reasons": reasons,
                "word_count": total_words,
                "orphan_count": len(orphans),
                "orphan_ratio": orphan_ratio,
                "pass_similarity": round(similarity, 4),
                "vad": {
                    "segments": len(spans),
                    "speech_sec": round(speech_sec, 2),
                    "audio_sec": round(total_sec, 2),
                    "speech_ratio": round(speech_sec / total_sec, 3) if total_sec else 0.0,
                    "spans": [[round(a, 3), round(b, 3)] for a, b in spans],
                },
                "pass_two_text": " ".join(pass_two_text),
                "orphans": orphans,
            },
        )

        ctx.tool = {"name": "onnx-asr + silero-vad", **runtime_info()}
        ctx.metrics = {
            "verdict": verdict,
            "orphan_count": len(orphans),
            "orphan_ratio": orphan_ratio,
            "pass_similarity": round(similarity, 4),
            "vad_segments": len(spans),
            "speech_ratio": round(speech_sec / total_sec, 3) if total_sec else 0.0,
        }
        ctx.log(f"вердикт {verdict}: " + ("; ".join(reasons) if reasons else "замечаний нет"))
        if verdict == FAIL:
            raise RuntimeError("проверка не пройдена: " + "; ".join(reasons))

    return execute_stage(
        lab_run,
        STAGE,
        inputs={"clean_16k": audio, "words": words_file},
        params=params,
        work=work,
        force=force,
    )
