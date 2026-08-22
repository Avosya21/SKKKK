from speakers import Turn

from merge_ensemble import pack_windows, _looks_tripled


def test_pack_windows_stay_in_120_180():
    turns = [
        Turn(0, 40, "Спикер №1", "a"),
        Turn(40, 80, "Спикер №2", "b"),
        Turn(80, 130, "Спикер №1", "c"),
        Turn(130, 170, "Спикер №2", "d"),
        Turn(170, 300, "Спикер №1", "e"),
        Turn(300, 340, "Спикер №2", "f"),
        Turn(340, 400, "Спикер №1", "g"),
    ]
    windows = pack_windows(turns, 400.0)
    assert windows[0][0] == 0.0
    assert abs(windows[-1][1] - 400.0) < 0.01
    for start, end in windows[:-1]:
        span = end - start
        assert span <= 180.5
        assert span >= 120


def test_whisper_triple_detector():
    once = "Да, я подключилась, всех слышу. Всё супер. " * 2
    triple = "Да, я подключилась, всех слышу. Всё супер. " * 5
    assert not _looks_tripled(once)
    assert _looks_tripled(triple)
