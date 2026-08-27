"""Сборка слов из токенов SentencePiece.

``onnx-asr`` отдаёт ``TimestampedResult`` с токенами и временем **начала**
каждого токена. Конца у токена нет, поэтому конец слова берём как начало
следующего токена, а у последнего — длительность аудио. Граница слова —
префикс ``▁`` в токене.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

WORD_MARKER = "\u2581"


def _token_piece(raw: str) -> tuple[bool, str]:
    """Граница слова: SentencePiece ``▁`` или ведущий пробел (Parakeet v3)."""
    if raw.startswith(WORD_MARKER):
        return True, raw[len(WORD_MARKER) :]
    if raw[:1].isspace() and raw.strip():
        return True, raw.lstrip()
    return False, raw


@dataclass
class Word:
    index: int
    text: str
    start: float
    end: float
    tokens: list[str] = field(default_factory=list)
    logprob: float | None = None

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "i": self.index,
            "word": self.text,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "tokens": self.tokens,
        }
        if self.logprob is not None:
            row["logprob"] = round(self.logprob, 4)
        return row


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def words_from_tokens(
    tokens: list[str],
    timestamps: list[float],
    *,
    audio_duration: float,
    logprobs: list[float] | None = None,
) -> list[Word]:
    """Слова с временем. Пустые токены и одиночные маркеры отбрасываются."""
    if len(tokens) != len(timestamps):
        raise ValueError(
            f"токенов {len(tokens)}, таймстампов {len(timestamps)} — рассинхрон onnx-asr"
        )

    groups: list[dict[str, Any]] = []
    for position, (raw, stamp) in enumerate(zip(tokens, timestamps, strict=True)):
        starts_word, piece = _token_piece(raw)
        if not piece and not groups:
            continue
        prob = logprobs[position] if logprobs and position < len(logprobs) else None
        if starts_word or not groups:
            groups.append(
                {"text": piece, "start": float(stamp), "tokens": [raw], "logprobs": []}
            )
        else:
            groups[-1]["text"] += piece
            groups[-1]["tokens"].append(raw)
        if prob is not None:
            groups[-1]["logprobs"].append(float(prob))

    words: list[Word] = []
    for order, group in enumerate(groups):
        text = group["text"].strip()
        if not text:
            continue
        nxt = groups[order + 1]["start"] if order + 1 < len(groups) else audio_duration
        end = max(float(group["start"]), float(nxt))
        words.append(
            Word(
                index=len(words),
                text=text,
                start=float(group["start"]),
                end=min(end, audio_duration) if audio_duration > 0 else end,
                tokens=group["tokens"],
                logprob=_mean(group["logprobs"]),
            )
        )
    return words


def words_to_text(words: list[Word]) -> str:
    return " ".join(w.text for w in words)


def audio_windows(
    duration: float,
    window: float,
    overlap: float,
) -> list[tuple[float, float]]:
    """Окна, покрывающие весь файл. overlap < window."""
    if duration <= 0:
        return [(0.0, 0.0)]
    if duration <= window:
        return [(0.0, duration)]
    step = window - overlap
    if step <= 0:
        raise ValueError("перекрытие окна должно быть меньше самого окна")
    spans: list[tuple[float, float]] = []
    start = 0.0
    while True:
        end = min(start + window, duration)
        spans.append((round(start, 6), round(end, 6)))
        if end >= duration:
            return spans
        start += step


def offset_words(words: list[Word], delta: float) -> list[Word]:
    return [
        Word(
            index=word.index,
            text=word.text,
            start=word.start + delta,
            end=word.end + delta,
            tokens=list(word.tokens),
            logprob=word.logprob,
        )
        for word in words
    ]


def cap_trailing_word(words: list[Word], max_sec: float = 1.5) -> list[Word]:
    """Последнее слово onnx-asr тянется до конца куска — обрезаем хвост."""
    if not words or max_sec <= 0:
        return words
    last = words[-1]
    span = last.end - last.start
    if span <= max_sec:
        return words
    typical = max_sec
    if len(words) >= 2:
        typical = min(max_sec, max(0.12, words[-2].end - words[-2].start))
    last.end = last.start + typical
    return words


def stitch_window_words(
    windows: list[tuple[float, float, list[Word]]],
    *,
    overlap: float,
) -> list[Word]:
    """Слова из перекрывающихся окон: берём по середине слова, без дыр."""
    if not windows:
        return []
    kept: list[Word] = []
    last_i = len(windows) - 1
    for index, (start, end, words) in enumerate(windows):
        lo = start if index == 0 else start + overlap / 2
        hi = end if index == last_i else end - overlap / 2
        for word in words:
            mid = (word.start + word.end) / 2
            if lo <= mid <= hi if index == last_i else lo <= mid < hi:
                kept.append(word)
    kept.sort(key=lambda word: (word.start, word.end))
    return [
        Word(
            index=order,
            text=word.text,
            start=word.start,
            end=word.end,
            tokens=list(word.tokens),
            logprob=word.logprob,
        )
        for order, word in enumerate(kept)
    ]
