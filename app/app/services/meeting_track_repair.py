"""Ремонт реплик дорожки встречи, когда основной распознаватель (Parakeet) ошибся во времени или в содержании.

Контекст (встреча, 21.09.2026). Дорожка участника — это его микрофон, и почти всё время он молчит. Parakeet на такой
дорожке ведёт себя плохо: молчит вовсе (Иван), выдаёт английские слова из шума с вырожденными временами (Пётр: слово
«do» длится 9 с, «people,» — 20 с) или несколько слов в конце записи (Олег: «So you're watching.» на 1,4 с). Вычитка
(`asr/lab_proof.py`) затем подгоняет верный текст второго слуха (GigaAM) под эти слова: все пять реплик Олега оказались
втиснуты в 1,4 секунды в конце, реплика Ивана пропала, Петру приписан английский текст.

У GigaAM время грубое (реплика целиком, разрешение около 2 с), но верное. Поэтому здесь:
  * реплика Parakeet после вычитки признаётся подозрительной, если текста слишком много для её длительности («схлопнута»)
    или слова вырождены (нулевые длительности, слово длиннее нескольких секунд);
  * подозрительные реплики отбрасываются, если у GigaAM есть свои реплики (иначе оставляем как есть: проверить нечем);
  * реплики GigaAM, не покрытые оставшимися репликами по времени, добавляются как есть, со временем GigaAM.
Всё, что отброшено или добавлено, возвращается в аудите для журнала. Чистые функции, без ввода-вывода.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

MAX_CHARS_PER_SEC = 25.0  # обычная речь ~10–15 знаков/с; выше — текст не мог быть сказан за это время
MIN_CHARS_FOR_RATE_CHECK = 20  # короткие реплики скоростью не проверяем: «Да.» за 0,3 с — нормально
MIN_DURATION_FOR_RATE_SEC = 0.5
LONG_WORD_SEC = 6.0  # слово длиннее — «длинное»: перед ним пауза или сбой разметки
LONG_WORDS_SHARE = 0.3  # доля времени реплики в длинных словах, с которой разметка считается вырожденной
DEGENERATE_ZERO_SHARE = 0.4  # доля слов с нулевой длительностью при не менее чем MIN_WORDS_FOR_ZERO_CHECK слов
MIN_WORDS_FOR_ZERO_CHECK = 5
ZERO_WORD_SEC = 0.05
COVER_TOLERANCE_SEC = 1.5  # разрешение времени у GigaAM ~2 с
MIN_WORD_SEC = 0.15

ACTIVE_THRESHOLD_DB = -48.0  # ниже — тишина/шум микрофона
ACTIVE_WINDOW_SEC = 0.5
COVERAGE_TOLERANCE_SEC = 2.0  # реплики GigaAM грубы по времени


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def suspect_reason(replica: dict) -> str | None:
    """Почему реплику дорожки нельзя считать достоверной по времени: `collapsed` / `degenerate` / None."""
    text = str(replica.get("text") or "").strip()
    start, end = _num(replica.get("start")), _num(replica.get("end"))
    duration = max(end - start, 0.0)
    if len(text) >= MIN_CHARS_FOR_RATE_CHECK and len(text) / max(duration, MIN_DURATION_FOR_RATE_SEC) > MAX_CHARS_PER_SEC:
        return "collapsed"
    words = [w for w in (replica.get("words") or []) if isinstance(w, dict)]
    if words:
        durations = [max(_num(w.get("end")) - _num(w.get("start")), 0.0) for w in words]
        # Одно длинное слово — обычная пауза после него (у нормальной реплики Елены на 309 с их два по 7 с, 5 % времени).
        # Вырождение — когда слова длиной в десятки секунд занимают заметную долю реплики (Пётр: 94 % времени).
        if duration > 0 and sum(d for d in durations if d > LONG_WORD_SEC) / duration >= LONG_WORDS_SHARE:
            return "degenerate"
        if len(words) >= MIN_WORDS_FOR_ZERO_CHECK and sum(1 for d in durations if d < ZERO_WORD_SEC) / len(words) >= DEGENERATE_ZERO_SHARE:
            return "degenerate"
    return None


def _covered(turn: dict, replicas: list[dict]) -> bool:
    t_start, t_end = _num(turn.get("start")), _num(turn.get("end"))
    for rep in replicas:
        if _num(rep.get("start")) - COVER_TOLERANCE_SEC <= t_end and _num(rep.get("end")) + COVER_TOLERANCE_SEC >= t_start:
            return True
    return False


def replica_from_turn(turn: dict) -> dict:
    """Реплика из реплики GigaAM. Неделима: весь текст — одно «слово» на шкале времени.

    У GigaAM время грубое (реплика «Всё готово.» помечена как 3–23 с). Если растянуть слова равномерно по такому отрезку, они
    перемежаются со словами других участников, и итоговая реплика рассыпается по одному слову. Одно слово-блок не рассыпается;
    длительность ограничена естественным темпом речи (0,5 с на слово + 1 с), а не грубым отрезком."""
    text = str(turn.get("text") or "").strip()
    start = _num(turn.get("start"))
    coarse_end = max(_num(turn.get("end"), start), start + MIN_WORD_SEC)
    natural = 0.5 * len(text.split()) + 1.0
    end = round(min(coarse_end, start + max(natural, MIN_WORD_SEC)), 3)
    word = {"i": 0, "word": text, "final": text, "start": round(start, 3), "end": end,
            "verdict": "assigned", "sense": None, "proof": "giga_anchor"}
    return {"speaker": str(turn.get("speaker") or "speaker_0"), "start": round(start, 3), "end": end,
            "text": text, "from_giga": len(text.split()), "words": [word]}


def repair_track_replicas(replicas: list[dict], giga_turns: list[dict]) -> tuple[list[dict], list[dict]]:
    """(починенные реплики по времени, аудит). Без реплик GigaAM реплики Parakeet возвращаются как есть."""
    if not giga_turns:
        return list(replicas), []
    audit: list[dict] = []
    kept: list[dict] = []
    for rep in replicas:
        reason = suspect_reason(rep)
        if reason:
            audit.append({"action": "dropped", "reason": reason, "start": _num(rep.get("start")), "end": _num(rep.get("end")),
                          "text": str(rep.get("text") or "")[:300]})
        else:
            kept.append(rep)
    result = list(kept)
    for turn in giga_turns:
        if not str(turn.get("text") or "").strip():
            continue
        if not _covered(turn, kept):
            result.append(replica_from_turn(turn))
            audit.append({"action": "added_from_giga", "start": _num(turn.get("start")), "end": _num(turn.get("end")),
                          "text": str(turn.get("text") or "")[:300]})
    result.sort(key=lambda r: (_num(r.get("start")), _num(r.get("end"))))
    return result, audit


# ───────────── Сверка «звук на микрофоне ↔ текст в стенограмме» ─────────────

def active_windows(wav_path: str | Path, offset_sec: float = 0.0, *, threshold_db: float = ACTIVE_THRESHOLD_DB,
                   window_sec: float = ACTIVE_WINDOW_SEC) -> list[tuple[float, float]]:
    """Интервалы (шкала встречи), где на дорожке есть звук громче порога. Пусто, если файла нет или он не читается."""
    import numpy as np

    try:
        with wave.open(str(wav_path), "rb") as fh:
            rate, channels, width = fh.getframerate(), fh.getnchannels(), fh.getsampwidth()
            frames = fh.readframes(fh.getnframes())
    except (OSError, EOFError, wave.Error):
        return []
    if width != 2 or not frames:
        return []
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples[: len(samples) // channels * channels].reshape(-1, channels).mean(axis=1)
    hop = max(int(rate * window_sec), 1)
    out: list[tuple[float, float]] = []
    for index in range(0, len(samples) - hop + 1, hop):
        rms = float(np.sqrt(np.mean(samples[index:index + hop] ** 2)))
        if 20.0 * np.log10(rms + 1e-9) > threshold_db:
            start = offset_sec + index / rate
            if out and abs(out[-1][1] - start) < 1e-6:
                out[-1] = (out[-1][0], start + window_sec)
            else:
                out.append((start, start + window_sec))
    return out


def track_coverage(speaker: str, windows: list[tuple[float, float]], replicas: list[dict], *,
                   tolerance_sec: float = COVERAGE_TOLERANCE_SEC) -> dict:
    """Сколько секунд звука на микрофоне участника не покрыто ни одной его репликой (с допуском по времени)."""
    mine = [(_num(r.get("start")) - tolerance_sec, _num(r.get("end")) + tolerance_sec) for r in replicas if r.get("speaker") == speaker]
    active = uncovered = 0.0
    for start, end in windows:
        length = end - start
        active += length
        if not any(lo <= end and hi >= start for lo, hi in mine):
            uncovered += length
    return {"speaker": speaker, "active_sec": round(active, 1), "uncovered_sec": round(uncovered, 1)}
