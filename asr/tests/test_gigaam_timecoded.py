from speakers import Turn
from timing_align import parse_gigaam_timecoded


def test_parse_gigaam_timecoded():
    raw = "[00:00 - 00:17] Первая фраза про свойства.\n[00:17 - 00:33] Вторая про модуль.\n"
    turns = parse_gigaam_timecoded(raw)
    assert len(turns) == 2
    assert turns[0].start == 0.0 and turns[0].end == 17.0
    assert "Первая" in turns[0].text
    assert turns[1].start == 17.0
