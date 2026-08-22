from hallucination_guard import (
    cap_text_to_speech_budget,
    collapse_adjacent_repeats,
    exceeds_density,
    looks_like_loop,
    prepare_engine_texts,
    sanitize_transcript_turns,
    scrub_engine_text,
    speech_budget_chars,
)
from speakers import Turn, apply_merged_windows, carve_text_by_speakers


def test_collapse_adjacent_sentence_repeat():
    text = "Да, там мы уже все сделали. Да, там мы уже все сделали."
    assert collapse_adjacent_repeats(text) == "Да, там мы уже все сделали."


def test_collapse_shifted_85_loop():
    text = "у нас осталась 85 -я. Еще, у нас осталась 85 -я. Еще,"
    out = collapse_adjacent_repeats(text)
    assert out.count("85") <= 1
    assert "Еще, у нас осталась 85" not in out or out.count("осталась") <= 1


def test_loop_detector_catches_whisper_spin():
    phrase = "Да, я подключилась, всех слышу. Всё супер. "
    assert not looks_like_loop(phrase * 2)
    assert looks_like_loop(phrase * 5)
    assert scrub_engine_text(phrase * 5) == ""


def test_density_impossible_on_screenshot_case():
    # ~75 слов за 4 секунды — как на баге в UI
    blob = (
        "Так вот это же расширение да у вас было оно позволяет экономить на лицензиях "
        "одинэс не пуская всех менеджеров в основную базу и нормализует учёт через обмен "
        "данными между системами битрикс и одинэс по номенклатуре и контрагентам "
    ) * 2
    assert exceeds_density(blob, 4.0)
    assert cap_text_to_speech_budget(blob, 4.0) == "" or len(cap_text_to_speech_budget(blob, 4.0)) <= 80


def test_prepare_does_not_chop_dual_engine_below_longest_draft():
    """Заниженный speech_sec не должен резать черновики ниже scrubbed max при 2+ движках."""
    tone = (
        "На созвоне разобрали установку обработок в базу двадцать пятого года. "
        "Потом отдельно проговорили отправку актов сверки и ошибку, из-за которой они не уходили. "
        "Ольга написала, что текст рассылки неправильный и идентификатор не подтянулся. "
        "Олег попросил открыть трекер и сравнить обработки из продуктивной базы с тестовой. "
        "Выяснилось, что стоят не последние версии доработок и пропала логика ошибки отправки. "
        "Нужно поставить ещё три файлика, где полный спектр доработок включая идентификаторы ЭДО. "
        "Елена уточнила, что после добавления трёх файлов появятся и остальные изменения. "
        "Дальше провалились по ссылке на Гугл Диск рядом со скрином в тикете. "
        "Дополнительно сверили колонку названия на диске и раздвинули ширину, чтобы видеть файлы."
    )
    giga = (
        "Коллеги подтвердили таймаут пятнадцать секунд для почтового сервиса. "
        "Отдельно проверили коннектор обмена и бакфиксы, которые уже лежат на диске у команды. "
        "Договорились не трогать боевую базу без согласованного окна. "
        "Оставили в чате обратную связь по восемьдесят пятому тикету после теста. "
        "Решили закрыть вопрос до отпуска и остальное перенести на возвращение. "
        "В конце пожелали хорошо отдохнуть и договорились вернуться к хвосту задач. "
        "Также отметили, что скрин в тикете нужно обновить после установки обработок."
    )
    tone_s = scrub_engine_text(tone) or tone
    giga_s = scrub_engine_text(giga) or giga
    assert len(tone_s) + len(giga_s) > speech_budget_chars(8.0) * 3
    # без пола обрезало бы до ~160 символов
    assert speech_budget_chars(8.0) < min(len(tone_s), len(giga_s))
    _, t2, g2, reason = prepare_engine_texts(
        "",
        tone,
        giga,
        speech_sec=8.0,
        window_span=150.0,
    )
    assert reason is None
    assert len(t2) >= len(tone_s) - 5
    assert len(g2) >= len(giga_s) - 5
    # Уникальный текст без петли — иначе сработает empty_or_loop раньше.
    whis_only = (
        "Нужно обсудить обмен номенклатуры между битрикс и одинэс, "
        "включая характеристики толщины длины и цвета для управленческого учёта."
    )
    whis, tone, giga, reason = prepare_engine_texts(
        whis_only,
        "",
        "",
        speech_sec=2.0,
        window_span=150.0,
    )
    assert reason == "single_engine_low_speech"
    assert whis == tone == giga == ""


def test_prepare_allows_consensus_with_speech():
    phrase = "Нужно синхронизировать номенклатуру из одинэс в битрикс."
    whis, tone, giga, reason = prepare_engine_texts(
        phrase,
        phrase,
        phrase,
        speech_sec=12.0,
        window_span=150.0,
    )
    assert reason is None
    assert whis and tone and giga


def test_pick_fallback_uses_gigaam_when_whisper_loops():
    from hallucination_guard import pick_fallback_engine_text

    loop = "На встрече всё обсудили. " * 40
    giga = (
        "В конце договорились вернуться к вопросу через неделю, "
        "сверить идентификаторы свойств на сайте и в одинэс, "
        "и закрыть хвост задач после отпуска коллег."
    )
    out = pick_fallback_engine_text(loop, "", giga, speech_sec=32.0)
    assert "идентификатор" in out or "неделю" in out
    assert "На встрече всё обсудили" not in out or out.count("На встрече") <= 1


def test_apply_merged_windows_drops_silence_novel():
    # Короткий «остров» диаризации + простыня Gemma = баг со скрина
    turns = [
        Turn(658.0, 662.0, "Спикер №2"),
        Turn(665.0, 671.0, "Спикер №3"),
    ]
    novel = (
        "Расширение экономит лицензии одинэс, менеджеры работают через битрикс, "
        "номенклатура и характеристики толщины длины цвета ведутся в учётной системе, "
        "мастер-системой должна быть одинэс а не битрикс иначе будет бардак с данными."
    )
    out = apply_merged_windows(turns, [(650.0, 800.0, novel)])
    assert not any(exceeds_density(t.text, t.duration) for t in out)
    # либо пусто, либо ужато до бюджета речи (~10 с → ≤200 символов суммарно)
    assert sum(len(t.text) for t in out) <= 220


def test_carve_also_caps_window_blob():
    speakers = [Turn(100.0, 104.0, "Спикер №1"), Turn(105.0, 111.0, "Спикер №2")]
    blob = "слово " * 200
    out = carve_text_by_speakers([Turn(90.0, 250.0, "", blob)], speakers)
    assert sum(len(t.text) for t in out) <= 220


def test_strip_merge_meta_drops_gemma_commentary():
    from hallucination_guard import scrub_merged_text, strip_merge_meta

    raw = (
        "Варианты не совпадают по смыслу и содержанию. Вероятно, это разные фрагменты записи.\n\n"
        "Автоматически оно там, ну, во-первых, это экономия на лицензиях 1С,"
    )
    cleaned = strip_merge_meta(raw)
    assert "Варианты не совпадают" not in cleaned
    assert "разные фрагменты" not in cleaned
    assert "экономия на лицензиях" in cleaned
    assert "разные фрагменты" not in scrub_merged_text(raw)

    turns = [
        Turn(804.0, 804.0, "Ольга", "производителя такого-то, цвета такого-то, и вы это как-то продаете."),
        Turn(810.0, 814.0, "Олег", "Да, согласен."),
    ]
    # densе-cut только по явному флагу (legacy); канон Gemma не режем
    assert len(sanitize_transcript_turns(turns)) == 2
    out = sanitize_transcript_turns(turns, drop_dense=True)
    assert len(out) == 1
    assert out[0].speaker == "Олег"
