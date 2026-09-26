from timing_align import (
    WordSpan,
    align_merged_to_timed,
    apply_gemma_to_phrase,
    collapse_whisper_word_cycles,
    pick_gemma_correction,
    strip_identical_phrase_runs,
    timed_asr_turns,
    turns_from_whisper_words,
)
from speakers import Turn


def test_turns_from_words_split_on_gap():
    words = [
        WordSpan("Привет", 1.0, 1.4),
        WordSpan("всем", 1.45, 1.8),
        WordSpan("Как", 5.0, 5.3),
        WordSpan("дела", 5.35, 5.7),
    ]
    turns, gaps = turns_from_whisper_words(words, collapse_cycles=False)
    assert gaps == []
    assert len(turns) == 2
    assert turns[0].start == 1.0


def test_gemma_text_on_whisper_clocks_not_whisper_words():
    """Текст в финале — из Gemma, даже если Whisper сказал другое в те же секунды."""
    timed = [
        Turn(10.0, 12.0, "", "шёпот про одно"),
        Turn(12.5, 15.0, "", "шёпот про другое"),
    ]
    speakers = [Turn(9.0, 16.0, "Спикер №1")]
    windows = [(9.0, 16.0, "Правильная склейка про коннектор и багфикс.")]
    out = align_merged_to_timed(timed, windows, speakers)
    joined = " ".join(t.text for t in out)
    assert "коннектор" in joined or "багфикс" in joined
    assert "шёпот" not in joined
    assert any(abs(t.start - 10.0) < 0.2 for t in out) or out[0].start >= 9.0


def test_align_keeps_full_gemma_when_longer_than_whisper():
    """Gemma длиннее Whisper — все предложения на месте, часы монотонны."""
    timed = [Turn(10.0, 11.0, "", "Привет")]
    speakers = [Turn(10.0, 30.0, "Спикер №1")]
    windows = [
        (
            10.0,
            30.0,
            "Первое предложение про акты. Второе про сверки и ЭДО. Третье про восьмидесятую задачу.",
        )
    ]
    out = align_merged_to_timed(timed, windows, speakers)
    joined = " ".join(t.text for t in out)
    assert "Первое" in joined and "Второе" in joined and "Третье" in joined
    assert "сверки" in joined and "восьмидесят" in joined
    starts = [t.start for t in out]
    assert starts == sorted(starts)
    for a, b in zip(out, out[1:]):
        assert b.start >= a.start - 0.05


def test_align_keeps_whisper_clocks_not_speaker_islands():
    timed = [
        Turn(10.0, 12.0, "", "ignored"),
        Turn(12.5, 15.0, "", "ignored"),
        Turn(20.0, 22.0, "", "ignored"),
    ]
    speakers = [
        Turn(9.0, 16.0, "Спикер №1"),
        Turn(19.0, 23.0, "Спикер №2"),
    ]
    windows = [(9.0, 23.0, "Первая фраза полностью. Вторая фраза здесь. Третья фраза конец.")]
    out = align_merged_to_timed(timed, windows, speakers)
    assert out
    assert all(t.duration <= 6.0 for t in out)
    assert {t.speaker for t in out} <= {"Спикер №1", "Спикер №2"}


def test_whisper_loop_gap_uses_tone_clocks_but_gemma_text():
    phrase = "Еще, у нас осталась 85-я."
    timed = [Turn(2000.0 + i * 2, 2001.0 + i * 2, "", phrase) for i in range(8)]
    speakers = [Turn(1995.0, 2030.0, "Спикер №1")]
    windows = [
        (
            1990.0,
            2030.0,
            "Разработчики коннектора заложили бакфиксы. Смотрим таймаут пятнадцать секунд.",
        )
    ]
    tone = [
        Turn(2002.0, 2010.0, "", "tone noise ignored"),
        Turn(2012.0, 2020.0, "", "more tone noise"),
        Turn(2022.0, 2028.0, "", "tail tone"),
    ]
    out = align_merged_to_timed(timed, windows, speakers, fallback_timed=tone)
    joined = " ".join(t.text for t in out)
    assert "коннектора" in joined or "таймаут" in joined or "бакфикс" in joined
    assert joined.count("85") <= 1
    # хвост окна не схлопывается в начало дыры
    assert max(t.end for t in out) >= 2018.0


def test_long_whisper_gap_spreads_gemma_across_tone_tail():
    """Петля Whisper на 8 минут: текст Gemma окон должен дойти до конца Tone, не застрять в начале дыры."""
    phrase = "Еще, у нас осталась 85-я."
    timed = [Turn(1980.0 + i, 1980.8 + i, "", phrase) for i in range(6)]
    speakers = [Turn(1970.0, 2490.0, "Спикер №1")]
    tone = [Turn(2000.0 + i * 20, 2005.0 + i * 20, "", f"tone{i}") for i in range(25)]
    windows = [
        (2110.0, 2270.0, "Первое окно про коннектор. Вторая мысль про таймаут."),
        (2270.0, 2430.0, "Третье окно про сверки. Четвёртая фраза про ЭДО."),
        (2430.0, 2489.0, "Хвост созвона: договорились созвониться. Всем спасибо."),
    ]
    out = align_merged_to_timed(timed, windows, speakers, fallback_timed=tone)
    joined = " ".join(t.text for t in out)
    assert "коннектор" in joined and "сверки" in joined and "спасибо" in joined
    assert max(t.end for t in out) >= 2450.0
    assert any(t.start >= 2400 for t in out)


def test_extra_word_cycle_gap_filled_with_gemma():
    timed = [Turn(1970.0, 1975.0, "", "Так, 81-я.")]
    speakers = [Turn(1970.0, 2100.0, "Спикер №1")]
    windows = [(1970.0, 2100.0, "Разработчики коннектора заложили бакфиксы. Таймаут пятнадцать.")]
    tone = [Turn(2000.0, 2010.0, "", "ignored tone text")]
    out = align_merged_to_timed(
        timed,
        windows,
        speakers,
        fallback_timed=tone,
        extra_gaps=[(1978.0, 2090.0)],
    )
    joined = " ".join(t.text for t in out)
    assert "коннектора" in joined or "коннектор" in joined or "таймаут" in joined
    assert "85" not in joined


def test_align_prefers_tone_text_times_after_longer_gemma():
    """Удлинённая Gemma в окне должна якориться по Tone-тексту, не сползать в начало."""
    timed = [Turn(100.0, 105.0, "", "шум в начале окна")]
    tone = [
        Turn(100.0, 110.0, "", "сначала говорим про коннектор и акты"),
        Turn(140.0, 150.0, "", "потом отдельно про таймаут пятнадцать секунд"),
        Turn(170.0, 180.0, "", "в конце прощаемся и до связи"),
    ]
    speakers = [Turn(100.0, 180.0, "Спикер №1")]
    windows = [
        (
            100.0,
            180.0,
            "Сначала говорим про коннектор и акты. Потом отдельно про таймаут пятнадцать секунд. В конце прощаемся и до связи.",
        )
    ]
    out = align_merged_to_timed(timed, windows, speakers, fallback_timed=tone)
    assert len(out) >= 3
    last = [t for t in out if "связи" in t.text or "проща" in t.text.lower()]
    assert last and last[0].start >= 160.0
    mid = [t for t in out if "таймаут" in t.text.lower()]
    assert mid and mid[0].start >= 130.0


def test_align_word_level_matches_gemma_tokens():
    words = [
        WordSpan("Разработчики", 100.0, 100.5),
        WordSpan("коннектора", 100.5, 101.0),
        WordSpan("заложили", 101.0, 101.4),
        WordSpan("бакфиксы", 101.4, 102.0),
        WordSpan("Таймаут", 110.0, 110.4),
        WordSpan("пятнадцать", 110.4, 111.0),
    ]
    timed = [Turn(100.0, 102.0, "", "Разработчики коннектора заложили бакфиксы")]
    speakers = [Turn(99.0, 120.0, "Спикер №1")]
    windows = [
        (
            99.0,
            120.0,
            "Разработчики коннектора заложили бакфиксы. Таймаут пятнадцать секунд.",
        )
    ]
    out = align_merged_to_timed(timed, windows, speakers, words=words)
    assert len(out) >= 2
    assert abs(out[0].start - 100.0) < 0.3
    assert abs(out[1].start - 110.0) < 0.5
    joined = " ".join(t.text for t in out)
    assert "бакфиксы" in joined and "пятнадцать" in joined


def test_align_upgrades_helpers_still_work():
    gemma = ["Да, там мы уже всё сделали.", "Другая тема про битрикс."]
    got = pick_gemma_correction("Да, там мы уже все сделали.", gemma)
    assert got is not None
    out = apply_gemma_to_phrase(
        "А. Б про перенос. В.",
        ["А исправлено.", "Б про перенос доработок.", "В конец."],
    )
    assert "перенос" in out


def test_pick_rejects_much_shorter_gemma():
    long_whisper = "Первые два файлика — багфикс. И остальные три — это перенос доработок по тикетам."
    assert pick_gemma_correction(long_whisper, ["Первые два файлика — багфикс."]) is None


def test_collapse_whisper_word_cycle_records_gap():
    cycle = ["Еще,", "у", "нас", "осталась", "85", "-я."]
    words: list[WordSpan] = []
    t = 2000.0
    for _ in range(30):
        for tok in cycle:
            words.append(WordSpan(tok, t, t + 0.15))
            t += 0.15
    collapsed, gaps = collapse_whisper_word_cycles(words)
    assert len(collapsed) == 6
    assert gaps and gaps[0][1] - gaps[0][0] > 10
    turns, turn_gaps = turns_from_whisper_words(words)
    assert turn_gaps
    assert " ".join(x.text for x in turns).count("85") <= 1


def test_strip_whisper_phrase_loop_family():
    variants = [
        "Еще, у нас осталась 85-я.",
        "у нас осталась 85-я. Еще,",
        "Еще, у нас осталась 85-я. Еще, у нас осталась 85-я. Еще,",
        "Еще, у нас осталась 85 -я.",
        "у нас осталась 85 -я. Еще,",
        "Еще, у нас осталась 85-я.",
    ]
    timed = [Turn(100.0 + i, 100.8 + i, "", variants[i % len(variants)]) for i in range(8)]
    timed.insert(0, Turn(90.0, 92.0, "", "Нормальный разговор про коннектор."))
    clean, gaps = strip_identical_phrase_runs(timed)
    assert sum(1 for t in clean if "85" in t.text) == 1
    assert gaps


def test_timed_asr_prefers_words_over_bloated_segment():
    payload = {
        "segments": [{"start": 0.0, "end": 97.0, "text": "Коротко."}],
        "words": [
            {"word": "Коротко.", "start": 0.5, "end": 1.2},
            {"word": "Дальше", "start": 3.0, "end": 3.4},
            {"word": "фраза", "start": 3.45, "end": 3.9},
        ],
    }
    turns, gaps = timed_asr_turns(payload)
    assert gaps == []
    assert turns[0].end <= 1.3


def test_align_preserves_gemma_sentence_order_when_clocks_shuffle():
    """Whisper-часы могут «перепутать» предложения — в UI текст Gemma остаётся по порядку."""
    timed = [
        Turn(10.0, 11.0, "", "шум"),
        Turn(30.0, 31.0, "", "шум"),
        Turn(50.0, 51.0, "", "шум"),
    ]
    speakers = [Turn(0.0, 60.0, "Спикер №1")]
    windows = [
        (
            0.0,
            60.0,
            "Первое предложение про акты. Второе про сверки. Третье про восьмидесятую задачу.",
        )
    ]
    out = align_merged_to_timed(timed, windows, speakers)
    texts = [t.text for t in out]
    joined = " | ".join(texts)
    i1 = joined.find("Первое")
    i2 = joined.find("Второе")
    i3 = joined.find("Третье")
    assert i1 != -1 and i2 != -1 and i3 != -1
    assert i1 < i2 < i3


def test_align_timestamps_monotonic_in_gemma_order():
    timed = [
        Turn(50.0, 51.0, "", "шум"),
        Turn(10.0, 11.0, "", "шум"),
        Turn(30.0, 31.0, "", "шум"),
    ]
    speakers = [Turn(0.0, 60.0, "Спикер №1")]
    windows = [
        (
            0.0,
            60.0,
            "Первое предложение про акты. Второе про сверки. Третье про восьмидесятую задачу.",
        )
    ]
    out = align_merged_to_timed(timed, windows, speakers)
    starts = [t.start for t in out]
    assert starts == sorted(starts)
    for a, b in zip(out, out[1:]):
        assert b.start >= a.start - 0.05


def test_nudge_monotonic_preserves_whisper_end_anchor():
    from timing_align import _nudge_monotonic_keep_order

    turns = [
        Turn(10.0, 14.0, "", "Первое."),
        Turn(12.0, 18.0, "", "Второе."),  # start раньше end первого — типичный shuffle
    ]
    out = _nudge_monotonic_keep_order(turns)
    assert out[0].start == 10.0 and out[0].end == 14.0
    assert out[1].start >= 14.0
    assert out[1].end >= 18.0
