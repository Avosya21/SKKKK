from speakers import Turn

from engine_timeline import tone_for_align


def test_tone_rescale_when_longer_but_same_call():
    whisper = [
        Turn(0.0, 30.0, "", "обсуждаем номенклатуру битрикс одинэс свойства"),
        Turn(30.0, 60.0, "", "продолжаем разговор про выгрузку"),
    ]
    tone = [
        Turn(0.0, 20.0, "", "обсуждаем номенклатуру битрикс одинэс свойства"),
        Turn(200.0, 220.0, "", "продолжаем разговор про выгрузку"),
    ]
    out = tone_for_align(tone, whisper, [], duration=100.0, log=False)
    assert out
    assert max(t.end for t in out) <= 100.5


def test_tone_reject_weak_overlap_even_if_duration_stretched():
    whisper = [Turn(0.0, 30.0, "", "коннектор обмена акты сверки битрикс")]
    tone = [Turn(0.0, 30.0, "", "совсем другая тема про погоду и отпуск")]
    out = tone_for_align(tone, whisper, [], duration=100.0, log=False)
    assert out == []


def test_tone_reject_foreign_call():
    whisper = [Turn(0.0, 5.0, "", "коннектор обмена акты сверки")]
    tone = [Turn(0.0, 5.0, "", "совсем другая тема про погоду и отпуск")]
    out = tone_for_align(tone, whisper, [], duration=100.0, log=False)
    assert out == []
