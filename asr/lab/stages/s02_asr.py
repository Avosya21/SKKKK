"""02_asr — Parakeet-TDT 0.6B v3 через onnx-asr.

Почему сначала текст, а не диаризация: если Sortformer пропустит тихую реплику,
то при обратном порядке она вообще не будет расшифрована. Здесь ASR слышит весь
файл, а спикеры накладываются позже.

TDT-декодер не умеет зацикливаться так, как авторегрессионный Whisper, поэтому
«придуманные фразы» на тишине лечатся здесь архитектурой, а не постфильтром.

ONNX-экспорт держит позиционные эмбеддинги ~9999 мел-кадров (~100 с). Длинный
созвон режется на окна с перекрытием: тихие куски не отбрасываются, это не VAD.
"""

from __future__ import annotations

from typing import Any

from ..audio import duration_sec, read_mono
from ..onnx_env import model_cache, providers_for, runtime_info
from ..paths import LabRun
from ..run import StageContext, execute_stage
from ..words import (
    Word,
    audio_windows,
    cap_trailing_word,
    offset_words,
    stitch_window_words,
    words_from_tokens,
    words_to_text,
)
from .s01_audio import clean_wav

STAGE = "02_asr"

WORDS_JSON = "words.json"
DEFAULT_MODEL = "nemo-parakeet-tdt-0.6b-v3"
# 9999 мел-кадров × 10 мс; оставляем запас под препроцессор.
MAX_WINDOW_SEC = 80.0
WINDOW_OVERLAP_SEC = 8.0


def words_path(run: LabRun) -> Path:
    return run.stage_dir(STAGE) / WORDS_JSON


def _result_to_words(result: Any, *, chunk_duration: float, offset: float) -> tuple[list[Word], int]:
    tokens = list(result.tokens or [])
    stamps = [float(t) for t in (result.timestamps or [])]
    if not tokens or not stamps:
        return [], 0
    words = words_from_tokens(
        tokens,
        stamps,
        audio_duration=chunk_duration,
        logprobs=[float(p) for p in (result.logprobs or [])] or None,
    )
    cap_trailing_word(words)
    return offset_words(words, offset), len(tokens)


def run(
    lab_run: LabRun,
    *,
    model_name: str = DEFAULT_MODEL,
    device: str = "auto",
    quantization: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    audio = clean_wav(lab_run)
    params: dict[str, Any] = {
        "model": model_name,
        "device": device,
        "quantization": quantization,
        "mode": "sliding_windows",
        "window_sec": MAX_WINDOW_SEC,
        "overlap_sec": WINDOW_OVERLAP_SEC,
    }

    def work(ctx: StageContext) -> None:
        import onnx_asr

        providers = providers_for(device)
        ctx.log(f"загрузка {model_name}, провайдеры {providers}")
        model = onnx_asr.load_model(
            model_name,
            str(model_cache("asr", model_name)),
            quantization=quantization,
            providers=providers,
        ).with_timestamps()

        samples, sample_rate = read_mono(audio)
        total = duration_sec(audio)
        spans = audio_windows(total, MAX_WINDOW_SEC, WINDOW_OVERLAP_SEC)
        ctx.log(f"распознавание {len(spans)} окон по {MAX_WINDOW_SEC:.0f} с, файл {total:.1f} с")

        window_rows: list[tuple[float, float, list[Word]]] = []
        token_count = 0
        model_parts: list[str] = []
        for index, (start, end) in enumerate(spans, start=1):
            begin = int(round(start * sample_rate))
            stop = int(round(end * sample_rate))
            chunk = samples[begin:stop]
            chunk_duration = max(end - start, 0.0)
            ctx.log(f"окно {index}/{len(spans)} {start:.1f}-{end:.1f} с")
            result = model.recognize(chunk, sample_rate=sample_rate)
            words, tokens = _result_to_words(result, chunk_duration=chunk_duration, offset=start)
            token_count += tokens
            if result.text:
                model_parts.append(str(result.text))
            window_rows.append((start, end, words))

        words = stitch_window_words(window_rows, overlap=WINDOW_OVERLAP_SEC)
        if not words:
            from .s03_diar import known_speaker_doc

            if not known_speaker_doc(lab_run.artifact_root):
                raise RuntimeError(
                    "onnx-asr не собрал ни одного слова — покрасить спикерами будет нечем"
                )
            ctx.log("native track: слов нет, дорожка без речи")

        payload = {
            "run_id": lab_run.run_id,
            "stage": STAGE,
            "engine": model_name,
            "audio": ctx.inputs["clean_16k"].as_dict(),
            "duration_sec": round(total, 2),
            "windows": [{"start": start, "end": end} for start, end, _ in window_rows],
            "model_text": " ".join(model_parts),
            "words_text": words_to_text(words),
            "word_count": len(words),
            "token_count": token_count,
            "words": [w.as_dict() for w in words],
        }
        ctx.write_json(WORDS_JSON, payload)

        speech = sum(w.end - w.start for w in words)
        ctx.tool = {"name": "onnx-asr", **runtime_info()}
        ctx.metrics = {
            "word_count": len(words),
            "token_count": token_count,
            "chars": len(payload["model_text"]),
            "windows": len(spans),
            "speech_sec": round(speech, 2),
            "speech_ratio": round(speech / total, 3) if total else 0.0,
            "first_word_at": round(words[0].start, 2) if words else None,
            "last_word_end": round(words[-1].end, 2) if words else None,
        }
        ctx.log(f"{len(words)} слов, {len(spans)} окон, речь {speech:.1f} с из {total:.1f} с")

    return execute_stage(
        lab_run,
        STAGE,
        inputs={"clean_16k": audio},
        params=params,
        work=work,
        force=force,
    )
