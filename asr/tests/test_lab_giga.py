"""Разбор ответа GigaAM: перекрытия и таймкоды не сглаживаем."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.giga import overlap_seconds, parse_turns, speaker_key, summarize  # noqa: E402

SAMPLE = """[00:00 - 00:06] Спикер №1: первая реплика
[00:06 - 00:31] Спикер №1: вторая реплика
[00:20 - 00:24] Спикер №2: перебил
[01:05 - 01:11] SPEAKER_03: третий голос
"""


def test_speaker_key_keeps_own_numbering():
    assert speaker_key("Спикер №2") == "giga_2"
    assert speaker_key("SPEAKER_03") == "giga_3"
    assert speaker_key("") == "giga:?"


def test_parse_turns_keeps_order_and_overlap():
    turns = parse_turns(SAMPLE)
    assert [t["speaker"] for t in turns] == ["giga_1", "giga_1", "giga_2", "giga_3"]
    assert turns[2]["start"] == 20.0
    assert turns[2]["text"] == "перебил"
    assert overlap_seconds(turns) == 4.0


def test_summary_counts_speakers_and_span():
    doc = summarize(parse_turns(SAMPLE), plain_text="первая реплика")
    assert doc["turn_count"] == 4
    assert doc["speaker_count"] == 3
    assert doc["span"] == {"start": 0.0, "end": 71.0}
    assert doc["speakers"][0]["speaker"] == "giga_1"
    assert doc["chars_plain"] == len("первая реплика")


def test_empty_input_is_not_a_crash():
    assert parse_turns("") == []
    assert summarize([])["turn_count"] == 0
