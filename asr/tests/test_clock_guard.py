from speakers import Turn
from clock_guard import assess_whisper_health, fallback_budget_sec, pick_clock_timed


def test_whisper_health_flags_loop_collapse():
    timed = [Turn(0, 5, "", "hello")] * 3
    gaps = [(300.0, 900.0)]
    h = assess_whisper_health(timed, gaps, duration=1800.0)
    assert not h.trusted
    assert "phrases_per_min" in h.reason or "gap_ratio" in h.reason


def test_pick_clock_prefers_tone_then_gigaam():
    tone = [Turn(0, 10, "", "a")]
    giga = [Turn(0, 20, "", "b")]
    out, src = pick_clock_timed(tone, giga, duration=100.0)
    assert src == "tone"
    assert out == tone
    out2, src2 = pick_clock_timed([], giga, duration=20.0)
    assert src2 == "gigaam"
    assert out2 == giga


def test_fallback_budget_uses_window_span():
    assert fallback_budget_sec(30.0, 108.0) >= 108.0 * 0.45
