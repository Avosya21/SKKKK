"""Оверлей смысловой разноски: не меняет текст, только спикера."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.sense import apply_overlay, disputed_spans, parse_decisions  # noqa: E402
from asr.lab.stages.s04_paint import ASSIGNED, CONTESTED, UNASSIGNED  # noqa: E402
from asr.lab.stages.s06_transcript import build_replicas  # noqa: E402


def _word(index, text, start, end, verdict, speaker):
    return {
        "i": index,
        "word": text,
        "start": start,
        "end": end,
        "verdict": verdict,
        "speaker_index": speaker,
        "speaker": None if speaker is None else f"speaker_{speaker}",
    }


def test_disputed_spans_split_on_assigned_and_pause():
    words = [
        _word(0, "да", 0.0, 0.2, ASSIGNED, 0),
        _word(1, "спор", 0.3, 0.5, CONTESTED, 0),
        _word(2, "ный", 0.5, 0.7, CONTESTED, 1),
        _word(3, "ок", 0.8, 1.0, ASSIGNED, 1),
        _word(4, "тихо", 5.0, 5.2, UNASSIGNED, None),
    ]
    spans = disputed_spans(words)
    assert [[w["word"] for w in span] for span in spans] == [["спор", "ный"], ["тихо"]]


def test_overlay_changes_speaker_not_text():
    words = [
        _word(0, "да", 0.0, 0.2, ASSIGNED, 0),
        _word(1, "нет", 0.3, 0.5, CONTESTED, 0),
    ]
    out = apply_overlay(words, [{"word_from": 1, "word_to": 1, "speaker_index": 1, "note": "ответ"}])
    assert out[1]["word"] == "нет"
    assert out[1]["start"] == 0.3
    assert out[1]["speaker_index"] == 1
    assert out[1]["sense"] is True
    assert out[0].get("sense") is None


def test_parse_decisions_rejects_unknown_speaker():
    rows = parse_decisions(
        {"decisions": [{"word_from": 1, "word_to": 2, "speaker_index": 9}]},
        allowed={0, 1},
    )
    assert rows == []
    rows = parse_decisions(
        {"decisions": [{"word_from": 1, "word_to": 2, "speaker": "speaker_1"}]},
        allowed={0, 1},
    )
    assert rows[0]["speaker_index"] == 1


def test_sense_transcript_keeps_all_words():
    words = [
        _word(0, "вопрос", 0.0, 0.4, ASSIGNED, 0),
        _word(1, "ответ", 0.5, 0.9, UNASSIGNED, None),
    ]
    painted = apply_overlay(words, [{"word_from": 1, "word_to": 1, "speaker_index": 1}])
    replicas = build_replicas(painted, max_gap=1.2, keep_unassigned=True)
    assert " ".join(r["text"] for r in replicas) == "вопрос ответ"
