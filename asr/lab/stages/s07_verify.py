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

Слово вне речи по VAD — ещё не выдумка. Под ним либо тишина (риск выдумки),
либо громкий звук, который VAD не принял за речь: голос, искажённый сбоем
связи, VAD и диаризация не слышат, а Parakeet разбирает. Провал проверки
считается только по словам на тишине; громкие участки — предупреждение с
временем (решение пользователя 2026-09-28, созвон проекта 40).
"""

from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import numpy as np

from ..audio import duration_sec, frame_levels_db
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

# Громкость под словом — самый громкий 50-мс кадр внутри него.
LEVEL_FRAME_SEC = 0.05
# Тишина — на 20 дБ тише обычной речи этой же записи (медиана слов, которые VAD подтвердил).
SILENCE_MARGIN_DB = 20.0
# Тише этого — тишина при любой громкости записи.
ALWAYS_SILENCE_DB = -50.0
# Опора, если VAD подтвердил слишком мало слов: тогда порог тишины −45 дБFS.
FALLBACK_SPEECH_DB = -25.0
MIN_REFERENCE_WORDS = 50
# Громкие слова вне речи сводятся в участки: соседние не дальше 15 с друг от друга.
LOUD_SPAN_GAP_SEC = 15.0
# В причину вердикта попадают участки хотя бы из 5 слов, не больше трёх.
LOUD_SPAN_MIN_WORDS = 5
LOUD_SPANS_IN_REASON = 3


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


def word_level_db(levels: np.ndarray, start: float, end: float, frame_sec: float = LEVEL_FRAME_SEC) -> float:
    """Громкость под словом: самый громкий кадр внутри него."""
    if levels.size == 0:
        return -120.0
    first = min(max(int(start / frame_sec), 0), levels.size - 1)
    last = max(first + 1, int(np.ceil(end / frame_sec)))
    return float(levels[first:last].max())


def silence_below_db(speech_levels: list[float]) -> float:
    """Порог тишины под словом: от уровня речи этой записи, но не ниже ``ALWAYS_SILENCE_DB``."""
    speech_db = float(np.median(speech_levels)) if len(speech_levels) >= MIN_REFERENCE_WORDS else FALLBACK_SPEECH_DB
    return max(speech_db - SILENCE_MARGIN_DB, ALWAYS_SILENCE_DB)


def loud_spans(orphans: list[dict[str, Any]], max_gap: float = LOUD_SPAN_GAP_SEC) -> list[dict[str, Any]]:
    """Слова вне речи на громком звуке, сведённые в участки времени."""
    spans: list[dict[str, Any]] = []
    for row in sorted(orphans, key=lambda item: float(item["start"])):
        start, end = float(row["start"]), float(row["end"])
        if spans and start - spans[-1]["end"] <= max_gap:
            spans[-1]["end"] = max(spans[-1]["end"], end)
            spans[-1]["words"] += 1
        else:
            spans.append({"start": start, "end": end, "words": 1})
    return [{"start": round(s["start"], 2), "end": round(s["end"], 2), "words": s["words"]} for s in spans]


def clock(sec: float) -> str:
    minutes, seconds = divmod(int(sec), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def verdict_of(
    *,
    total_words: int,
    silent_count: int,
    loud: list[dict[str, Any]],
    similarity: float,
    max_silence_ratio: float,
) -> tuple[str, list[str], float]:
    """Вердикт и причины. Провал — только доля слов на тишине выше порога."""
    silence_ratio = round(silent_count / total_words, 4) if total_words else 0.0
    verdict = OK
    reasons: list[str] = []
    if silent_count:
        verdict = WARN
        reasons.append(f"слов на тишине вне речи по VAD: {silent_count}")
    if loud:
        verdict = WARN
        spans = [s for s in loud_spans(loud) if s["words"] >= LOUD_SPAN_MIN_WORDS]
        spans = sorted(sorted(spans, key=lambda s: -s["words"])[:LOUD_SPANS_IN_REASON], key=lambda s: s["start"])
        where = ", ".join(f"{clock(s['start'])}–{clock(s['end'])}" for s in spans)
        reasons.append(
            f"слов на громком звуке, где VAD не услышал речь: {len(loud)}" + (f" ({where})" if where else "")
        )
    if silence_ratio > max_silence_ratio:
        verdict = FAIL
        reasons.append(f"доля слов на тишине {silence_ratio} выше порога {max_silence_ratio}")
    if similarity < 0.80:
        verdict = FAIL if verdict == FAIL else WARN
        reasons.append(f"проходы расходятся, сходство {similarity:.2f}")
    return verdict, reasons, silence_ratio


def run(
    lab_run: LabRun,
    *,
    device: str = "auto",
    model_name: str = DEFAULT_MODEL,
    vad_threshold: float = 0.5,
    min_silence_ms: float = 300.0,
    speech_pad_ms: float = 120.0,
    min_coverage: float = 0.10,
    max_silence_ratio: float = 0.02,
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
        "max_silence_ratio": max_silence_ratio,
        "silence": {
            "frame_sec": LEVEL_FRAME_SEC,
            "margin_db": SILENCE_MARGIN_DB,
            "always_below_db": ALWAYS_SILENCE_DB,
            "fallback_speech_db": FALLBACK_SPEECH_DB,
            "min_reference_words": MIN_REFERENCE_WORDS,
        },
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
        levels = frame_levels_db(audio, LEVEL_FRAME_SEC)
        orphans: list[dict[str, Any]] = []
        speech_levels: list[float] = []
        for word in words_doc["words"]:
            start, end = float(word["start"]), float(word["end"])
            coverage = covered_ratio(start, end, spans)
            level = word_level_db(levels, start, end)
            if coverage > min_coverage:
                speech_levels.append(level)
                continue
            orphans.append(
                {
                    "i": word["i"],
                    "word": word["word"],
                    "start": word["start"],
                    "end": word["end"],
                    "vad_coverage": round(coverage, 3),
                    "level_db": round(level, 1),
                }
            )

        silence_db = silence_below_db(speech_levels)
        for row in orphans:
            row["silence"] = row["level_db"] < silence_db
        loud = [row for row in orphans if not row["silence"]]
        silent_count = len(orphans) - len(loud)

        total_words = len(words_doc["words"])
        orphan_ratio = round(len(orphans) / total_words, 4) if total_words else 0.0
        similarity = SequenceMatcher(
            None, _normalize(words_doc.get("words_text", "")), _normalize(" ".join(pass_two_text))
        ).ratio()

        speech_sec = sum(stop - start for start, stop in spans)
        total_sec = duration_sec(audio)
        verdict, reasons, silence_ratio = verdict_of(
            total_words=total_words,
            silent_count=silent_count,
            loud=loud,
            similarity=similarity,
            max_silence_ratio=max_silence_ratio,
        )

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
                "silence_orphan_count": silent_count,
                "silence_orphan_ratio": silence_ratio,
                "loud_orphan_count": len(loud),
                "loud_spans": loud_spans(loud),
                "levels": {
                    "speech_db": round(float(np.median(speech_levels)), 1) if speech_levels else None,
                    "reference_words": len(speech_levels),
                    "silence_below_db": round(silence_db, 1),
                },
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
            "silence_orphan_count": silent_count,
            "silence_orphan_ratio": silence_ratio,
            "loud_orphan_count": len(loud),
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
