"""Вычитка текста: выбор только из двух услышанных вариантов."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.proof import (  # noqa: E402
    apply_picks,
    collect_diffs,
    final_replicas,
    norm_token,
    parse_picks,
)


def word(index, start, speaker, text):
    return {
        "i": index,
        "start": start,
        "end": start + 0.4,
        "word": text,
        "verdict": "assigned",
        "speaker_index": speaker,
        "speaker": f"speaker_{speaker}",
    }


def turn(index, start, end, text, speaker="giga_4"):
    return {"i": index, "start": start, "end": end, "speaker": speaker, "text": text}


def test_norm_token_folds_case_yo_and_latin_lookalikes():
    assert norm_token("Ещё,") == norm_token("еще")
    assert norm_token("1С") == norm_token("1C")


def test_replace_diff_carries_both_variants_and_context():
    words = [
        word(0, 0.0, 1, "нет,"),
        word(1, 1.0, 1, "ширина"),
        word(2, 2.0, 1, "стандарта."),
        word(3, 3.0, 1, "Для"),
    ]
    turns = [turn(0, 0.0, 9.0, "Нет, ширина стандартная. Режем")]
    diffs = collect_diffs(words, turns)
    replace = [row for row in diffs if row["op"] == "replace"]
    assert replace[0]["lab"] == "стандарта. Для"
    assert replace[0]["giga"] == "стандартная. Режем"
    assert replace[0]["left"].endswith("ширина")
    assert replace[0]["cosmetic"] is False


def test_case_only_difference_is_marked_cosmetic():
    words = [word(0, 0.0, 1, "битрикс"), word(1, 1.0, 1, "работает")]
    turns = [turn(0, 0.0, 5.0, "Битрикс работает")]
    diffs = collect_diffs(words, turns)
    assert all(row["cosmetic"] for row in diffs)


def test_different_tokenisation_is_cosmetic_too():
    """Parakeet отдаёт «в 1С» одним токеном, GigaAM — двумя. Слух тот же."""
    words = [word(0, 0.0, 1, "в 1С"), word(1, 1.0, 1, "пришла")]
    turns = [turn(0, 0.0, 5.0, "в 1С пришла")]
    diffs = collect_diffs(words, turns)
    assert diffs and all(row["cosmetic"] for row in diffs)


def test_parse_picks_drops_free_text_and_unknown_ids():
    raw = {
        "decisions": [
            {"id": "d1:1", "pick": "giga"},
            {"id": "d1:1", "pick": "GIGA"},
            {"id": "d9:9", "pick": "lab"},
            {"id": "d2:2", "pick": "свой вариант"},
        ]
    }
    assert parse_picks(raw, {"d1:1", "d2:2"}) == {"d1:1": "giga"}


def test_apply_picks_replaces_only_chosen_range():
    words = [word(0, 0.0, 1, "ширина"), word(1, 1.0, 1, "стандарта.")]
    diffs = [
        {
            "op": "replace",
            "pick": "giga",
            "word_from": 1,
            "word_to": 1,
            "giga": "стандартная.",
        }
    ]
    out = apply_picks(words, diffs)
    assert [row["final"] for row in out] == ["ширина", "стандартная."]
    assert out[1]["proof"] == "giga"
    assert out[1]["word"] == "стандарта."


def test_apply_picks_keeps_lab_when_not_chosen():
    words = [word(0, 0.0, 1, "гонять")]
    diffs = [{"op": "replace", "pick": "lab", "word_from": 0, "word_to": 0, "giga": "глинять"}]
    assert apply_picks(words, diffs)[0]["final"] == "гонять"


def test_insert_is_attached_to_neighbour_word():
    words = [word(0, 0.0, 1, "работает."), word(1, 1.0, 1, "Нет,")]
    diffs = [
        {"op": "insert", "pick": "giga", "word_from": None, "word_to": None,
         "anchor": 0, "anchor_mode": "after", "giga": "вот."},
        {"op": "insert", "pick": "giga", "word_from": None, "word_to": None,
         "anchor": 0, "anchor_mode": "before", "giga": "ну"},
    ]
    out = apply_picks(words, diffs)
    assert out[0]["final"] == "ну работает. вот."
    assert out[0]["start"] == 0.0


def test_multi_word_replace_collapses_into_head():
    words = [word(n, float(n), 1, f"w{n}") for n in range(3)]
    diffs = [{"op": "replace", "pick": "giga", "word_from": 0, "word_to": 2, "giga": "один"}]
    out = apply_picks(words, diffs)
    assert [row["final"] for row in out] == ["один", "", ""]


def test_final_replicas_split_by_speaker_and_skip_emptied_words():
    words = [
        {**word(0, 0.0, 1, "раз"), "final": "раз"},
        {**word(1, 0.5, 1, "два"), "final": ""},
        {**word(2, 1.0, 0, "три"), "final": "три"},
    ]
    rows = final_replicas(words)
    assert [row["speaker"] for row in rows] == ["speaker_1", "speaker_0"]
    assert rows[0]["text"] == "раз"
    assert rows[1]["text"] == "три"


def test_final_replicas_keep_original_times():
    words = [{**word(0, 5.0, 1, "раз"), "final": "совсем другое слово"}]
    row = final_replicas(words)[0]
    assert row["start"] == 5.0
    assert row["end"] == 5.4
