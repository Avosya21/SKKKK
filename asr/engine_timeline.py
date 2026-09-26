"""Фильтрация таймлайнов ASR-движков перед склейкой и alignment."""

from __future__ import annotations

import re

from speakers import Turn


def engine_span(segs: list[Turn]) -> float:
    if not segs:
        return 0.0
    return max(t.end for t in segs)


def _head_text(segs: list[Turn], until: float = 90.0) -> str:
    parts = [t.text for t in segs if t.start < until and (t.text or "").strip()]
    return " ".join(parts).lower()


def _lexical_overlap(a_segs: list[Turn], b_segs: list[Turn], until: float = 90.0) -> float | None:
    a_head = _head_text(a_segs, until)
    b_head = _head_text(b_segs, until)
    if not a_head or not b_head or len(a_head) <= 40 or len(b_head) <= 40:
        return None
    a_tokens = {tok for tok in re.findall(r"[а-яёa-z0-9]{4,}", a_head)}
    b_tokens = {tok for tok in re.findall(r"[а-яёa-z0-9]{4,}", b_head)}
    if not a_tokens or not b_tokens:
        return None
    return len(a_tokens & b_tokens) / max(1, len(a_tokens))


def tone_for_align(
    tone: list[Turn],
    whisper: list[Turn],
    gigaam: list[Turn],
    duration: float,
    *,
    log: bool = True,
) -> list[Turn]:
    """Tone для часов: отбрасываем чужой созвон или растянутый таймлайн без якоря."""
    if not tone:
        return []
    ref = duration or engine_span(whisper) or engine_span(gigaam)
    tone_end = engine_span(tone)
    if ref > 60 and tone_end > 0 and abs(ref - tone_end) > max(45.0, ref * 0.05):
        overlap = _lexical_overlap(tone, whisper or gigaam)
        if overlap is not None and overlap >= 0.15 and tone_end > ref * 1.05:
            scale = ref / tone_end
            scaled = [
                Turn(
                    start=t.start * scale,
                    end=min(t.end * scale, ref),
                    speaker=t.speaker,
                    text=t.text,
                )
                for t in tone
                if t.start * scale < ref - 0.05
            ]
            if scaled and log:
                print(
                    f"tone RESCALE {tone_end:.0f}s -> {ref:.0f}s overlap={overlap:.2f}",
                    flush=True,
                )
            return scaled
        if log:
            print(
                f"tone REJECT duration mismatch: tone_end={tone_end:.0f}s ref={ref:.0f}s "
                f"(похоже на чужой docker_tone)",
                flush=True,
            )
        return []

    overlap = _lexical_overlap(tone, whisper or gigaam)
    if overlap is not None and overlap < 0.08:
        if log:
            print(
                f"tone REJECT lexical mismatch at start: overlap={overlap:.2f} "
                f"(Tone не про этот аудиофайл)",
                flush=True,
            )
        return []
    return tone
