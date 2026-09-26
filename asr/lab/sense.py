"""Смысловая разноска спорных фраз — оверлей над зафиксированной покраской.

Не стадия конвейера 01–07: не трогает words.json / paint.json / transcript.json.
Пишет только ``sense/`` внутри того же прогона.
"""

from __future__ import annotations

from typing import Any

from .stages.s04_paint import ASSIGNED, CONTESTED, UNASSIGNED

DISPUTED = {CONTESTED, UNASSIGNED}
CONTEXT_WORDS = 14
BATCH_SPANS = 10
MAX_INTERNAL_GAP = 1.5


def disputed_spans(
    words: list[dict[str, Any]],
    *,
    max_internal_gap: float = MAX_INTERNAL_GAP,
) -> list[list[dict[str, Any]]]:
    """Непрерывные куски contested/unassigned. Пауза длиннее gap рвёт кусок."""
    spans: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for word in words:
        if word.get("verdict") in DISPUTED:
            if current and float(word["start"]) - float(current[-1]["end"]) > max_internal_gap:
                spans.append(current)
                current = []
            current.append(word)
        elif current:
            spans.append(current)
            current = []
    if current:
        spans.append(current)
    return spans


def _fmt_words(words: list[dict[str, Any]]) -> str:
    parts = []
    for word in words:
        speaker = word.get("speaker") or "?"
        parts.append(f"{word['i']}:{speaker}:{word['word']}")
    return " ".join(parts)


def span_payload(
    words: list[dict[str, Any]],
    span: list[dict[str, Any]],
    *,
    context: int = CONTEXT_WORDS,
) -> dict[str, Any]:
    by_index = {int(word["i"]): word for word in words}
    first = int(span[0]["i"])
    last = int(span[-1]["i"])
    left = [by_index[i] for i in range(max(0, first - context), first) if i in by_index]
    right = [by_index[i] for i in range(last + 1, last + 1 + context) if i in by_index]
    candidates = sorted(
        {
            int(word["speaker_index"])
            for word in left + span + right
            if word.get("speaker_index") is not None
        }
    )
    return {
        "word_from": first,
        "word_to": last,
        "start": span[0]["start"],
        "end": span[-1]["end"],
        "text": " ".join(word["word"] for word in span),
        "verdicts": [word.get("verdict") for word in span],
        "diar_guess": [word.get("speaker") for word in span],
        "left": _fmt_words(left),
        "right": _fmt_words(right),
        "candidates": candidates,
    }


def batched(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def parse_decisions(raw: Any, *, allowed: set[int]) -> list[dict[str, Any]]:
    """Только индексы слов и спикер из уже известных. Текст не принимаем."""
    if isinstance(raw, dict):
        raw = raw.get("decisions") or raw.get("spans") or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            word_from = int(row["word_from"])
            word_to = int(row["word_to"])
        except (KeyError, TypeError, ValueError):
            continue
        if word_to < word_from:
            continue
        speaker = row.get("speaker_index", row.get("speaker"))
        if isinstance(speaker, str) and speaker.startswith("speaker_"):
            try:
                speaker = int(speaker.split("_", 1)[1])
            except ValueError:
                continue
        if speaker is None or speaker == "":
            index = None
        else:
            try:
                index = int(speaker)
            except (TypeError, ValueError):
                continue
            if index not in allowed:
                continue
        out.append(
            {
                "word_from": word_from,
                "word_to": word_to,
                "speaker_index": index,
                "note": str(row.get("note") or "")[:240],
            }
        )
    return out


def apply_overlay(words: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Копия слов: меняется только спикер, текст и время неприкосновенны."""
    overlay: dict[int, dict[str, Any]] = {}
    for decision in decisions:
        if decision.get("speaker_index") is None:
            continue
        for index in range(int(decision["word_from"]), int(decision["word_to"]) + 1):
            overlay[index] = decision
    result: list[dict[str, Any]] = []
    for word in words:
        row = dict(word)
        hit = overlay.get(int(word["i"]))
        if hit is not None:
            speaker = int(hit["speaker_index"])
            row["speaker_index"] = speaker
            row["speaker"] = f"speaker_{speaker}"
            row["sense"] = True
            row["sense_note"] = hit.get("note") or ""
        result.append(row)
    return result
