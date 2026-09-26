from speakers import (
    Turn,
    apply_map,
    apply_merged_windows,
    canonical_speaker,
    carve_text_by_speakers,
    drop_minor_speakers,
    flatten_overlaps,
    drop_contained_islands,
    format_turns_timecodes,
    merge_speakers,
    overlay_text,
    parse_timecode_lines,
    reattach_cut_letters,
    renumber_speakers,
    smooth_islands,
    split_text_by_weights,
    talk_time,
)


def test_parse_and_canonical():
    text = (
        "[00:00 - 00:51] Спикер №1: привет\n"
        "[00:51 - 01:09] SPEAKER_04: счета\n"
        "[1:09 - 1:12] без метки\n"
    )
    turns = parse_timecode_lines(text)
    assert len(turns) == 3
    assert turns[0].start == 0
    assert turns[0].end == 51
    assert canonical_speaker(turns[0].speaker) == "Спикер №1"
    assert turns[1].speaker == "Спикер №4"
    assert turns[2].speaker == ""
    assert turns[2].text == "без метки"


def test_smooth_aba_island():
    turns = [
        Turn(0.0, 10.0, "Спикер №1", "раз"),
        Turn(10.0, 10.4, "Спикер №2", "угу"),
        Turn(10.4, 20.0, "Спикер №1", "два"),
    ]
    out = smooth_islands(turns, min_duration=0.8)
    assert len(out) == 1
    assert out[0].speaker == "Спикер №1"
    assert out[0].start == 0.0
    assert out[0].end == 20.0
    assert "угу" in out[0].text


def test_overlay_assigns_by_overlap():
    speakers = [
        Turn(0.0, 5.0, "Спикер №1"),
        Turn(5.0, 10.0, "Спикер №2"),
    ]
    words = [
        Turn(0.2, 4.8, "", "алёна говорит"),
        Turn(5.1, 9.0, "", "влад отвечает"),
    ]
    out = overlay_text(words, speakers)
    assert [t.speaker for t in out] == ["Спикер №1", "Спикер №2"]
    assert out[0].text == "алёна говорит"


def test_rename_merge_and_talk_time():
    turns = [
        Turn(0.0, 4.0, "Спикер №1", "а"),
        Turn(4.0, 6.0, "Спикер №2", "б"),
        Turn(6.0, 10.0, "Спикер №1", "в"),
    ]
    named = apply_map(turns, {"Спикер №1": "Алёна", "Спикер №2": "Пётр"})
    assert {t.speaker for t in named} == {"Алёна", "Пётр"}
    merged = merge_speakers(named, "Пётр", "Алёна")
    assert {t.speaker for t in merged} == {"Алёна"}
    stats = talk_time(turns)
    assert stats[0]["speaker"] == "Спикер №1"
    assert stats[0]["seconds"] == 8.0


def test_timecode_format_roundtrip_minutes():
    turns = [Turn(65.0, 70.2, "Спикер №3", "ок")]
    line = format_turns_timecodes(turns).strip()
    back = parse_timecode_lines(line)
    assert back[0].speaker == "Спикер №3"
    assert back[0].start == 65
    assert back[0].text == "ок"


def test_split_text_prefers_punctuation():
    text = "Первая фраза. Вторая фраза полностью. Третья."
    parts = split_text_by_weights(text, [10.0, 10.0])
    assert len(parts) == 2
    assert parts[0].endswith(".")
    assert "Третья" in parts[1]


def test_apply_merged_windows_keeps_speakers():
    turns = [
        Turn(0.0, 80.0, "Спикер №1", "aaaaaaaaaa"),
        Turn(80.0, 160.0, "Спикер №2", "bbbbbbbbbb"),
    ]
    out = apply_merged_windows(turns, [(0.0, 160.0, "Привет. Ответ.")])
    assert [t.speaker for t in out] == ["Спикер №1", "Спикер №2"]
    assert out[0].text
    assert out[1].text


def test_carve_splits_18s_chunk_on_speaker_change():
    audio = [Turn(0.0, 18.0, "", "Женщина говорит долго потом мужчина отвечает ей")]
    speakers = [
        Turn(0.0, 10.0, "Спикер №1"),
        Turn(10.0, 18.0, "Спикер №2"),
    ]
    out = carve_text_by_speakers(audio, speakers)
    assert [t.speaker for t in out] == ["Спикер №1", "Спикер №2"]
    assert out[0].end == 10.0
    assert out[1].start == 10.0
    assert out[0].text
    assert out[1].text


def test_split_skips_tiny_weight_letter():
    text = "Это в первую очередь учитывается."
    parts = split_text_by_weights(text, [0.01, 20.0])
    assert not any(p.strip() in {"Э", "т"} for p in parts)
    assert any("Это" in p or "это" in p.lower() for p in parts if p)


def test_tiny_pyannote_blip_does_not_steal_letter():
    turns = [
        Turn(0.0, 80.0, "Спикер №1"),
        Turn(80.0, 80.05, "Спикер №3"),
        Turn(80.05, 160.0, "Спикер №2"),
    ]
    out = apply_merged_windows(
        turns, [(0.0, 160.0, "Это в первую очередь учитывается. Дальше идёт длинный текст.")]
    )
    assert not any(len(t.text.strip()) <= 2 and t.text.strip().isalpha() for t in out)
    assert not any(t.text.strip() == "Э" for t in out)


def test_reattach_letter_to_next_word():
    turns = [
        Turn(10.0, 10.05, "Спикер №3", "Э"),
        Turn(10.05, 12.0, "Спикер №2", "то в первую очередь учитывается."),
    ]
    out = reattach_cut_letters(turns)
    assert out[0].text.startswith("Это")
    assert not any(t.text.strip() == "Э" for t in out)


def test_reattach_conjunction_with_space():
    turns = [
        Turn(12.0, 12.2, "Спикер №4", "И"),
        Turn(12.2, 13.0, "Спикер №2", "поэтому как бы есть необходимость"),
    ]
    out = reattach_cut_letters(turns)
    assert out[0].text.startswith("И поэтому")


def test_flatten_keeps_long_speaker():
    turns = [
        Turn(0.0, 40.0, "Спикер №1"),
        Turn(10.0, 13.0, "Спикер №2"),
    ]
    out = flatten_overlaps(turns)
    assert len(out) == 1
    assert out[0].speaker == "Спикер №1"
    assert out[0].start == 0.0
    assert out[0].end == 40.0


def test_flatten_switches_when_previous_ends():
    turns = [
        Turn(0.0, 10.0, "Спикер №1"),
        Turn(10.0, 30.0, "Спикер №2"),
    ]
    out = flatten_overlaps(turns)
    speakers = [(round(t.start), round(t.end), t.speaker) for t in out]
    assert speakers == [(0, 10, "Спикер №1"), (10, 30, "Спикер №2")]


def test_contained_island_dropped():
    turns = [
        Turn(0.0, 40.0, "Спикер №1"),
        Turn(10.0, 13.0, "Спикер №2"),
        Turn(40.0, 50.0, "Спикер №2"),
    ]
    out = drop_contained_islands(turns)
    assert [(round(t.start), round(t.end), t.speaker) for t in out] == [
        (0, 40, "Спикер №1"),
        (40, 50, "Спикер №2"),
    ]


def test_drop_minor_maps_whole_cluster():
    turns = [
        Turn(0.0, 30.0, "Спикер №1"),
        Turn(30.0, 32.0, "Спикер №9"),
        Turn(32.0, 62.0, "Спикер №2"),
        Turn(80.0, 110.0, "Спикер №2"),
        Turn(110.0, 111.0, "Спикер №9"),
    ]
    cleaned = drop_minor_speakers(turns, min_seconds=5.0)
    leftover = {t.speaker for t in cleaned}
    assert leftover == {"Спикер №1", "Спикер №2"}
    nine_slots = [t.speaker for t in cleaned if 29.0 <= t.start <= 32.5 or 109.0 <= t.start <= 111.5]
    assert len(set(nine_slots)) <= 1


def test_drop_minor_and_renumber():
    turns = [
        Turn(0.0, 20.0, "Спикер №1", "а"),
        Turn(20.0, 21.0, "Спикер №9", "шум"),
        Turn(21.0, 40.0, "Спикер №2", "б"),
    ]
    cleaned = drop_minor_speakers(turns, min_seconds=5.0)
    assert {t.speaker for t in cleaned} == {"Спикер №1", "Спикер №2"}
    numbered = renumber_speakers(cleaned)
    assert numbered[0].speaker == "Спикер №1"
    assert numbered[-1].speaker == "Спикер №2"
