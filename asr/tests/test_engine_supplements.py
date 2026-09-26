from speakers import Turn
from timing_align import _engine_supplements_for_window


def test_engine_supplement_fills_gemma_hole_from_clock():
    gemma = "Елена говорит про сайт. Вот."
    clock = [
        Turn(400.0, 418.0, "", "Ну, давайте глянем демо-базу, чтобы была наглядность."),
        Turn(418.0, 436.0, "", "У тебя версия, которая появилась до 1С."),
    ]
    aligned = [
        Turn(380.0, 395.0, "", "Елена говорит про сайт."),
        Turn(395.0, 396.0, "", "Вот."),
    ]
    extra = _engine_supplements_for_window(gemma, clock, 360.0, 480.0, aligned)
    assert len(extra) >= 1
    assert any("глянем" in t.text for t in extra)
