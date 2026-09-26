"""Имена с боя — подписи, не нарезка."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.hint import leftover_words, map_lab_speakers_to_prod_names  # noqa: E402
from asr.lab.stages.s04_paint import ASSIGNED, UNASSIGNED  # noqa: E402


def _word(index, text, start, end, verdict, speaker, *, sense=False):
    return {
        "i": index,
        "word": text,
        "start": start,
        "end": end,
        "verdict": verdict,
        "speaker_index": speaker,
        "speaker": None if speaker is None else f"speaker_{speaker}",
        **({"sense": True} if sense else {}),
    }


def test_names_follow_majority_not_prod_cuts():
    words = [
        _word(0, "а", 0.0, 1.0, ASSIGNED, 0),
        _word(1, "б", 1.0, 2.0, ASSIGNED, 0),
        _word(2, "в", 10.0, 11.0, ASSIGNED, 1),
        _word(3, "хвост", 50.0, 51.0, UNASSIGNED, None),
    ]
    turns = [
        {"start": 0.0, "end": 3.0, "speaker": "Директор Пилота", "text": "аб"},
        {"start": 9.0, "end": 12.0, "speaker": "Елена Соколова", "text": "в"},
        {"start": 20.0, "end": 21.0, "speaker": "Игорь Пилот", "text": "мимо"},
    ]
    doc = map_lab_speakers_to_prod_names(words, turns)
    assert doc["speakers"]["0"]["display_name"] == "Директор Пилота"
    assert doc["speakers"]["1"]["display_name"] == "Елена Соколова"
    assert leftover_words(words)[0]["word"] == "хвост"
    assert doc["leftover"][0]["closed"] is False


def test_weak_vote_is_not_a_name_and_does_not_merge():
    words = [
        _word(0, "а", 0.0, 1.0, ASSIGNED, 3),
        _word(1, "б", 1.0, 2.0, ASSIGNED, 3),
        _word(2, "в", 2.0, 3.0, ASSIGNED, 3),
    ]
    turns = [
        {"start": 0.0, "end": 1.5, "speaker": "Елена Соколова", "text": "а"},
        {"start": 1.5, "end": 3.5, "speaker": "Директор Пилота", "text": "бв"},
    ]
    doc = map_lab_speakers_to_prod_names(words, turns, min_share=0.85)
    assert doc["speakers"]["3"]["display_name"] is None
    assert doc["speakers"]["3"]["accepted"] is False
