from pathlib import Path

from app.app.services.meeting_merge import (
    apply_leaks,
    find_leaks,
    format_meeting_clock,
    merge_replicas,
    merge_track_replicas,
    merge_words,
    replicas_from_words,
    shift_replicas,
    shift_words,
    word_text,
    words_from_replicas,
)


def test_shift_and_merge_keeps_names_and_time_order():
    anna = shift_replicas(
        [{"start": 1.0, "end": 2.0, "text": "привет", "speaker": "speaker_0"}],
        10.0,
        "Анна",
    )
    ivan = shift_replicas(
        [{"start": 0.5, "end": 1.5, "text": "здравствуйте", "speaker": "speaker_0"}],
        0.0,
        "Иван",
    )
    merged = merge_replicas(anna, ivan)
    assert [row["speaker"] for row in merged] == ["Иван", "Анна"]
    assert merged[0]["start"] == 0.5
    assert merged[1]["start"] == 11.0
    assert merged[1]["text"] == "привет"


def test_shift_drops_empty_text():
    assert shift_replicas([{"start": 0, "end": 1, "text": "  ", "speaker": "x"}], 0, "А") == []


def test_word_timeline_keeps_interrupt_order():
    anna = shift_words(
        [
            {"word": "давай", "start": 1.0, "end": 1.2},
            {"word": "сделаем", "start": 3.0, "end": 3.4},
        ],
        0.0,
        "Анна",
    )
    ivan = shift_words(
        [{"word": "секунду", "start": 1.5, "end": 2.0}],
        0.0,
        "Иван",
    )
    tape = merge_words(anna, ivan)
    assert [row["final"] for row in tape] == ["давай", "секунду", "сделаем"]
    assert [row["speaker"] for row in tape] == ["Анна", "Иван", "Анна"]
    turns = replicas_from_words(tape)
    assert [row["speaker"] for row in turns] == ["Анна", "Иван", "Анна"]
    assert turns[1]["text"] == "секунду"
    assert format_meeting_clock(turns[1]["start"]) == "00:02"


def test_shift_words_adds_join_offset():
    rows = shift_words([{"word": "привет", "start": 1.0, "end": 1.4}], 10.0, "Даша")
    assert rows[0]["start"] == 11.0
    assert rows[0]["end"] == 11.4
    assert rows[0]["speaker"] == "Даша"


def test_words_from_replicas_keep_nested_times():
    words = words_from_replicas(
        [
            {
                "start": 0.0,
                "end": 4.0,
                "text": "давай сделаем",
                "words": [
                    {"word": "давай", "start": 0.0, "end": 0.4},
                    {"word": "сделаем", "start": 3.0, "end": 3.5},
                ],
            }
        ]
    )
    assert [row["word"] for row in words] == ["давай", "сделаем"]
    assert words[1]["start"] == 3.0


def test_source_turn_id_is_stable():
    from app.app.services.meeting_merge import attach_turn_ids, source_turn_id

    assert source_turn_id("art_1", "Анна", 1.5, 0) == "turn:art_1:Анна:1500:0"
    rows = attach_turn_ids([{"speaker": "Анна", "start": 1.5, "text": "раз"}], "art_1")
    assert rows[0]["source_span_id"] == "turn:art_1:Анна:1500:0"


def test_final_replica_has_source_span_and_track_identity():
    from app.app.services.meeting_merge import attach_turn_ids

    rows = attach_turn_ids(
        merge_track_replicas(
            [
                {
                    "word": "готово",
                    "final": "готово",
                    "start": 2.0,
                    "end": 2.4,
                    "speaker": "Анна",
                    "track_id": "art__member-1",
                    "identity": "member:1",
                }
            ]
        ),
        "art_1",
    )
    assert rows[0]["source_span_id"] == "turn:art_1:Анна:2000:0"
    assert rows[0]["track_id"] == "art__member-1"
    assert rows[0]["identity"] == "member:1"


def test_word_text_keeps_empty_final():
    assert word_text({"final": "", "word": "один"}) == ""
    assert word_text({"final": None, "word": "один"}) == "один"
    assert word_text({"word": "один"}) == "один"


def test_merge_track_replicas_reads_as_dialogue():
    dasha = [
        {"word": "Алло.", "final": "Алло.", "start": 171.7, "end": 174.9, "speaker": "Даша"},
        {"word": "Подожди", "final": "Подожди", "start": 178.9, "end": 179.6, "speaker": "Даша"},
        {"word": "слышно.", "final": "слышно.", "start": 180.0, "end": 180.4, "speaker": "Даша"},
    ]
    petya = [
        {"word": "Я", "final": "Я", "start": 173.6, "end": 174.0, "speaker": "Петя"},
        {"word": "слышу.", "final": "слышу.", "start": 174.4, "end": 177.2, "speaker": "Петя"},
        {"word": "Алло", "final": "Алло", "start": 185.1, "end": 185.8, "speaker": "Петя"},
    ]
    rows = merge_track_replicas(dasha, petya)
    assert [row["speaker"] for row in rows] == ["Даша", "Петя", "Даша", "Петя"]
    assert rows[0]["text"] == "Алло."
    assert "Подожди" in rows[2]["text"]


def test_merge_track_replicas_keeps_overlap_phrases():
    anna = [
        {"i": 0, "word": "один", "final": "один", "start": 10.0, "end": 10.4, "speaker": "A"},
        {"i": 1, "word": "два", "final": "два", "start": 10.5, "end": 11.0, "speaker": "A"},
        {"i": 2, "word": "три", "final": "три", "start": 11.1, "end": 11.5, "speaker": "A"},
    ]
    ivan = [
        {"i": 0, "word": "раз", "final": "раз", "start": 10.4, "end": 10.7, "speaker": "B"},
        {"i": 1, "word": "два", "final": "два", "start": 10.7, "end": 11.0, "speaker": "B"},
    ]
    rows = merge_track_replicas(anna, ivan)
    assert [row["speaker"] for row in rows] == ["A", "B", "A", "B", "A"]
    assert rows[0]["text"] == "один"
    assert rows[1]["text"] == "раз"


def test_merge_track_replicas_preserves_track_provenance():
    rows = merge_track_replicas(
        [
            {
                "word": "привет",
                "final": "привет",
                "start": 1.0,
                "end": 1.4,
                "speaker": "Анна",
                "track_id": "art__guest-aa",
                "identity": "guest:aa",
            }
        ]
    )
    assert rows[0]["track_id"] == "art__guest-aa"
    assert rows[0]["identity"] == "guest:aa"
    assert rows[0]["words"][0]["track_id"] == "art__guest-aa"
    assert rows[0]["words"][0]["identity"] == "guest:aa"


def test_find_leaks_uses_speaker_rank():
    first = [
        {"word": "один", "final": "один", "start": 34.2, "end": 34.6, "speaker": "guest:A"},
        {"word": "два", "final": "два", "start": 34.6, "end": 35.0, "speaker": "guest:A"},
    ]
    second = [
        {"word": "один", "final": "один", "start": 34.25, "end": 34.65, "speaker": "guest:B"},
        {"word": "два", "final": "два", "start": 34.65, "end": 35.05, "speaker": "guest:B"},
    ]
    events = [{"t": 34.2, "kind": "speakers", "active": ["guest:A"]}]
    leaks = find_leaks([first, second], events)
    assert leaks
    assert leaks[0]["speaker"] == "guest:B"
    assert leaks[0]["from_speaker"] == "guest:A"
    assert leaks[0]["by"] == "rank"
    cleaned = apply_leaks([first, second], leaks)
    assert [word_text(row) for row in cleaned[1]] == []


def test_find_leaks_ignores_far_apart_matches():
    first = [
        {"word": "один", "final": "один", "start": 1.0, "end": 1.2, "speaker": "A"},
        {"word": "два", "final": "два", "start": 1.2, "end": 1.4, "speaker": "A"},
    ]
    second = [
        {"word": "один", "final": "один", "start": 8.0, "end": 8.2, "speaker": "B"},
        {"word": "два", "final": "два", "start": 8.2, "end": 8.4, "speaker": "B"},
    ]
    assert find_leaks([first, second]) == []


def test_mix_single_track_copies(tmp_path: Path):
    from app.app.services.meeting_merge import mix_track_wavs

    src = tmp_path / "a.wav"
    src.write_bytes(b"RIFF" + b"\x00" * 12)
    dest = tmp_path / "mix.wav"
    mix_track_wavs([(src, 0.0)], dest)
    assert dest.read_bytes() == src.read_bytes()
