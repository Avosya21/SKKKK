"""Сверка с GigaAM: закрываем спор только по однородной реплике."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.reconcile import (  # noqa: E402
    CLOSED,
    CONFIRMED,
    CONFLICT,
    OPEN,
    UNVERIFIED,
    disputed_spans,
    judge_spans,
    map_speakers,
    text_windows,
    turn_purity,
)


def word(index, start, speaker, text, verdict="assigned", sense=False):
    row = {
        "i": index,
        "start": start,
        "end": start + 0.4,
        "word": text,
        "verdict": verdict,
        "speaker_index": speaker,
        "speaker": None if speaker is None else f"speaker_{speaker}",
    }
    if sense:
        row["sense"] = True
    return row


def turn(index, start, end, speaker, text=""):
    return {
        "i": index,
        "start": start,
        "end": end,
        "speaker": speaker,
        "speaker_label": speaker,
        "text": text,
        "chars": len(text),
    }


def pure_run(speaker, giga_speaker, *, base=0.0, count=6, start_index=0):
    """Однородный кусок: шесть уверенных слов одного спикера внутри одной реплики."""
    words = [
        word(start_index + n, base + n, speaker, f"w{n}")
        for n in range(count)
    ]
    turns = [turn(0, base - 1.0, base + count + 1.0, giga_speaker)]
    return words, turns


def test_map_speakers_allows_many_giga_to_one_lab():
    words = [word(n, n, 1, "w") for n in range(12)]
    turns = [turn(0, -1, 6, "giga_4"), turn(1, 6, 20, "giga_2")]
    mapping = map_speakers(words, turns)
    assert mapping["giga_4"]["speaker_index"] == 1
    assert mapping["giga_2"]["speaker_index"] == 1


def test_map_speakers_rejects_weak_majority():
    words = [word(n, n, n % 2, "w") for n in range(12)]
    turns = [turn(0, -1, 20, "giga_7")]
    assert map_speakers(words, turns)["giga_7"]["speaker_index"] is None


def test_turn_purity_marks_dialogue_inside_one_turn():
    words = [word(n, n, 0 if n < 5 else 1, "w") for n in range(10)]
    turns = [turn(0, -1, 20, "giga_4")]
    row = turn_purity(turns, words)[0]
    assert row["pure"] is False
    assert row["purity"] == 0.5


def test_disputed_spans_split_by_gap():
    words = [
        word(0, 0.0, None, "a", verdict="contested"),
        word(1, 0.5, None, "b", verdict="contested"),
        word(2, 40.0, None, "c", verdict="unassigned"),
    ]
    spans = disputed_spans(words)
    assert [len(span) for span in spans] == [2, 1]


def test_span_closed_when_giga_turn_is_pure():
    words, turns = pure_run(1, "giga_4", count=8)
    words.append(word(99, 4.05, None, "спорное", verdict="contested"))
    words.sort(key=lambda row: row["start"])
    mapping = map_speakers(words, turns)
    rows = judge_spans(words, turns, mapping)
    assert [row["verdict"] for row in rows] == [CLOSED]
    assert rows[0]["giga"]["speaker_index"] == 1


def test_span_conflict_when_giga_disagrees_with_stage_two():
    words, turns = pure_run(1, "giga_4", count=8)
    words.append(word(99, 4.05, 0, "спорное", verdict="contested", sense=True))
    words.sort(key=lambda row: row["start"])
    mapping = map_speakers(words, turns)
    rows = judge_spans(words, turns, mapping)
    assert rows[0]["verdict"] == CONFLICT
    assert rows[0]["from_sense"] is True


def test_span_confirmed_when_giga_agrees():
    words, turns = pure_run(1, "giga_4", count=8)
    words.append(word(99, 4.05, 1, "спорное", verdict="contested", sense=True))
    words.sort(key=lambda row: row["start"])
    rows = judge_spans(words, turns, map_speakers(words, turns))
    assert rows[0]["verdict"] == CONFIRMED


def test_mixed_giga_turn_cannot_close_anything():
    # Спикер GigaAM в целом сопоставлен, но конкретная реплика — диалог двух наших.
    words = [word(n, n, 0 if n < 5 else 1, "w") for n in range(10)]
    words += [word(20 + n, 30.0 + n, 1, "w") for n in range(10)]
    words.append(word(99, 4.5, None, "спорное", verdict="unassigned"))
    words.sort(key=lambda row: row["start"])
    turns = [turn(0, -1, 20, "giga_4"), turn(1, 29.0, 45.0, "giga_4")]
    mapping = map_speakers(words, turns)
    assert mapping["giga_4"]["speaker_index"] == 1
    rows = judge_spans(words, turns, mapping)
    assert rows[0]["verdict"] == OPEN
    assert "смешанная" in rows[0]["giga"]["reason"]


def test_answered_span_without_giga_opinion_is_unverified():
    words = [word(n, n, 1, "w") for n in range(8)]
    words.append(word(99, 100.0, 1, "спорное", verdict="contested", sense=True))
    turns = [turn(0, -1, 10, "giga_4")]
    rows = judge_spans(words, turns, map_speakers(words, turns))
    assert rows[0]["verdict"] == UNVERIFIED


def test_text_windows_score_similarity():
    words = [
        word(0, 1.0, 1, "ширина"),
        word(1, 2.0, 1, "стандарта"),
    ]
    turns = [turn(0, 0.0, 5.0, "giga_4", "Ширина стандартная")]
    row = text_windows(words, turns)[0]
    assert row["our_words"] == 2
    assert row["agree"] is False
    assert 0.0 < row["similarity"] < 1.0
