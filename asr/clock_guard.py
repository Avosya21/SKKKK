"""Диагностика петель Whisper и выбор надёжного якоря часов для alignment."""

from __future__ import annotations

from dataclasses import dataclass

from speakers import Turn

# ~5 фраз/мин на нормальном созвоне; ниже 2.5 — типичный collapse после петли Whisper.
MIN_PHRASES_PER_MIN = 2.5
# Доля записи в «дырах» после среза петли — выше порога не доверяем Whisper-часам.
MAX_GAP_RATIO = 0.12


@dataclass(frozen=True)
class WhisperHealth:
    phrase_count: int
    gap_count: int
    gap_sec: float
    duration_sec: float
    phrases_per_min: float
    gap_ratio: float
    trusted: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "phrase_count": self.phrase_count,
            "gap_count": self.gap_count,
            "gap_sec": round(self.gap_sec, 1),
            "duration_sec": round(self.duration_sec, 1),
            "phrases_per_min": round(self.phrases_per_min, 2),
            "gap_ratio": round(self.gap_ratio, 3),
            "whisper_trusted": self.trusted,
            "reason": self.reason,
        }


def assess_whisper_health(
    timed: list[Turn],
    gaps: list[tuple[float, float]],
    duration: float,
) -> WhisperHealth:
    dur = max(float(duration), 1.0)
    gap_sec = sum(max(0.0, ge - gs) for gs, ge in (gaps or []))
    ppm = len(timed) / (dur / 60.0)
    gap_ratio = gap_sec / dur
    reasons: list[str] = []
    if dur >= 300 and ppm < MIN_PHRASES_PER_MIN:
        reasons.append(f"phrases_per_min={ppm:.1f}<{MIN_PHRASES_PER_MIN}")
    if gap_ratio > MAX_GAP_RATIO:
        reasons.append(f"gap_ratio={gap_ratio:.2f}>{MAX_GAP_RATIO}")
    if len(gaps or []) >= 5 and gap_sec > 60:
        reasons.append(f"whisper_gaps={len(gaps)}")
    trusted = not reasons
    return WhisperHealth(
        phrase_count=len(timed),
        gap_count=len(gaps or []),
        gap_sec=gap_sec,
        duration_sec=dur,
        phrases_per_min=ppm,
        gap_ratio=gap_ratio,
        trusted=trusted,
        reason="ok" if trusted else "; ".join(reasons),
    )


def pick_clock_timed(
    tone_timed: list[Turn],
    gigaam_clock: list[Turn],
    *,
    duration: float,
) -> tuple[list[Turn], str]:
    """Каскад часов: Tone (уже проверен) → GigaAM timecodes → пусто."""
    if tone_timed:
        return tone_timed, "tone"
    if gigaam_clock:
        end = max(t.end for t in gigaam_clock)
        if duration <= 0 or end >= duration * 0.85:
            return gigaam_clock, "gigaam"
        # усечённый GigaAM — всё равно лучше, чем петля Whisper
        return gigaam_clock, "gigaam_partial"
    return [], "none"


def fallback_budget_sec(speech_sec: float, window_span: float) -> float:
    """Бюджет текста для engine-fallback: diar часто занижает хвост окна."""
    span = max(float(window_span), 0.0)
    speech = max(float(speech_sec), 0.0)
    if span <= 0:
        return speech
    # не меньше ~45% окна и не меньше diar-бюджета
    return max(speech, span * 0.45)
