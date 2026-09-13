"""Логика стадий лаборатории без моделей: слова, покраска, реплики, клипы, VAD."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr.lab.sortformer import Binarization, binarize  # noqa: E402
from asr.lab.stages.s04_paint import ASSIGNED, CONTESTED, UNASSIGNED, classify, word_frames  # noqa: E402
from asr.lab.stages.s05_voice import clips_from_runs, word_runs  # noqa: E402
from asr.lab.stages.s06_transcript import build_replicas  # noqa: E402
from asr.lab.stages.s07_verify import covered_ratio  # noqa: E402
from asr.lab.words import words_from_tokens, words_to_text  # noqa: E402

MARKER = "\u2581"


def test_words_assembled_from_subword_tokens():
    tokens = [f"{MARKER}При", "вет", f"{MARKER}мир", "!"]
    stamps = [0.10, 0.30, 0.60, 0.80]
    words = words_from_tokens(tokens, stamps, audio_duration=1.5)

    assert [w.text for w in words] == ["Привет", "мир!"]
    assert words[0].start == pytest.approx(0.10)
    assert words[0].end == pytest.approx(0.60)
    assert words[1].end == pytest.approx(1.5)
    assert words_to_text(words) == "Привет мир!"


def test_words_assembled_from_leading_space_tokens():
    tokens = [" к", " ка", "ким", "-", "то", " со", "бы", "ти", "ям"]
    stamps = [0.10, 0.20, 0.28, 0.35, 0.40, 0.50, 0.58, 0.65, 0.72]
    words = words_from_tokens(tokens, stamps, audio_duration=1.0)
    assert [w.text for w in words] == ["к", "каким-то", "событиям"]


def test_words_reject_desynced_timestamps():
    with pytest.raises(ValueError):
        words_from_tokens([f"{MARKER}раз", "два"], [0.1], audio_duration=1.0)


def test_word_averages_token_logprobs():
    words = words_from_tokens(
        [f"{MARKER}при", "вет"], [0.0, 0.2], audio_duration=1.0, logprobs=[-0.2, -0.4]
    )
    assert words[0].logprob == pytest.approx(-0.3)


def test_audio_windows_cover_long_file_without_gaps():
    from asr.lab.words import Word, audio_windows, stitch_window_words

    spans = audio_windows(250.0, 80.0, 8.0)
    assert spans[0] == (0.0, 80.0)
    assert spans[-1][1] == pytest.approx(250.0)
    assert all(end - start <= 80.0001 for start, end in spans)
    for prev, cur in zip(spans, spans[1:]):
        assert cur[0] < prev[1]


def test_stitch_windows_keeps_midpoint_owner():
    from asr.lab.words import Word, stitch_window_words

    left = [Word(0, "раз", 70.0, 71.0), Word(1, "два", 78.0, 79.0)]
    right = [Word(0, "два", 78.1, 79.1), Word(1, "три", 82.0, 83.0)]
    merged = stitch_window_words([(0.0, 80.0, left), (72.0, 152.0, right)], overlap=8.0)
    assert [w.text for w in merged] == ["раз", "два", "три"]
    assert merged[1].start == pytest.approx(78.1)


def test_word_frames_cover_at_least_one_frame():
    assert word_frames(0.0, 0.01, 100) == (0, 1)
    assert word_frames(0.80, 0.96, 100) == (10, 12)
    first, last = word_frames(999.0, 999.5, 100)
    assert (first, last) == (99, 100)


def test_classify_three_buckets():
    verdict, speaker, _, _ = classify(
        np.array([0.9, 0.05, 0.0, 0.0]), min_prob=0.35, min_margin=0.15
    )
    assert (verdict, speaker) == (ASSIGNED, 0)

    verdict, speaker, _, margin = classify(
        np.array([0.52, 0.48, 0.0, 0.0]), min_prob=0.35, min_margin=0.15
    )
    assert verdict == CONTESTED and speaker == 0 and margin < 0.15

    verdict, speaker, _, _ = classify(
        np.array([0.05, 0.02, 0.0, 0.0]), min_prob=0.35, min_margin=0.15
    )
    assert (verdict, speaker) == (UNASSIGNED, None)


def _painted(rows):
    return [
        {
            "i": index,
            "word": word,
            "start": start,
            "end": end,
            "verdict": verdict,
            "speaker_index": speaker,
            "confidence": 0.9,
        }
        for index, (word, start, end, verdict, speaker) in enumerate(rows)
    ]


def test_replicas_split_on_speaker_and_pause():
    words = _painted(
        [
            ("раз", 0.0, 0.4, ASSIGNED, 0),
            ("два", 0.5, 0.9, ASSIGNED, 0),
            ("три", 1.0, 1.4, ASSIGNED, 1),
            ("четыре", 9.0, 9.4, ASSIGNED, 1),
        ]
    )
    replicas = build_replicas(words, max_gap=1.2, keep_unassigned=True)
    assert [r["speaker"] for r in replicas] == ["speaker_0", "speaker_1", "speaker_1"]
    assert replicas[0]["text"] == "раз два"


def test_empty_paint_is_not_a_hard_failure():
    from asr.lab.stages.s06_transcript import replicas_from_paint

    assert replicas_from_paint({"words": []}, max_gap=1.2, keep_unassigned=True) == []


def test_painted_words_without_replicas_still_fail():
    from asr.lab.stages.s06_transcript import EMPTY_REPLICAS, replicas_from_paint

    painted = _painted([("шум", 0.0, 0.2, UNASSIGNED, None)])
    with pytest.raises(RuntimeError, match=EMPTY_REPLICAS):
        replicas_from_paint({"words": painted}, max_gap=1.2, keep_unassigned=False)


def test_unassigned_words_are_kept_and_flagged():
    words = _painted(
        [
            ("норм", 0.0, 0.4, ASSIGNED, 0),
            ("шум", 0.5, 0.7, UNASSIGNED, None),
        ]
    )
    kept = build_replicas(words, max_gap=1.2, keep_unassigned=True)
    assert [r["speaker"] for r in kept] == ["speaker_0", "unknown"]
    assert kept[1]["flagged"] == 1

    dropped = build_replicas(words, max_gap=1.2, keep_unassigned=False)
    assert [r["speaker"] for r in dropped] == ["speaker_0"]


def test_word_runs_break_on_gap_and_verdict():
    words = _painted(
        [
            ("раз", 0.0, 0.4, ASSIGNED, 0),
            ("два", 0.5, 0.9, ASSIGNED, 0),
            ("спорно", 1.0, 1.2, CONTESTED, 0),
            ("три", 1.3, 1.7, ASSIGNED, 0),
        ]
    )
    runs = word_runs(words, max_gap=0.6)
    assert len(runs) == 2
    assert runs[0]["words"] == 2
    assert runs[1]["text"] == "три"


def test_clips_trim_edges_and_cap_length():
    runs = [
        {"speaker_index": 0, "start": 0.0, "end": 30.0, "words": 40, "text": "длинный"},
        {"speaker_index": 0, "start": 40.0, "end": 41.0, "words": 2, "text": "короткий"},
        {"speaker_index": 1, "start": 50.0, "end": 56.0, "words": 9, "text": "второй"},
    ]
    clips = clips_from_runs(
        runs, edge_trim=0.25, min_duration=2.0, max_duration=12.0, per_speaker=3
    )
    assert set(clips) == {"spk0_0", "spk1_0"}
    assert clips["spk0_0"]["duration"] == pytest.approx(12.0)
    assert clips["spk1_0"]["start"] == pytest.approx(50.25)


def test_covered_ratio_detects_word_in_silence():
    spans = [(0.0, 1.0), (5.0, 6.0)]
    assert covered_ratio(0.2, 0.6, spans) == pytest.approx(1.0)
    assert covered_ratio(3.0, 3.4, spans) == pytest.approx(0.0)
    assert covered_ratio(0.8, 1.2, spans) == pytest.approx(0.5)


def test_binarize_uses_hysteresis_and_merges_short_gaps():
    probabilities = np.zeros((40, 4), dtype=np.float32)
    probabilities[5:15, 0] = 0.9
    probabilities[16:25, 0] = 0.9
    segments = binarize(
        probabilities,
        rules=Binarization(pad_onset=0.0, min_duration_off=0.15),
        audio_duration=40 * 0.08,
    )
    assert len(segments) == 1
    assert segments[0]["speaker"] == "speaker_0"
    assert segments[0]["start"] == pytest.approx(0.40)


def test_binarize_ignores_weak_probabilities():
    probabilities = np.full((30, 4), 0.3, dtype=np.float32)
    assert binarize(probabilities, audio_duration=2.4) == []


def test_native_track_diar_is_one_known_speaker():
    from asr.lab.sortformer import FRAME_DURATION, NUM_SPEAKERS
    from asr.lab.stages.s03_diar import native_track_probabilities, native_track_segments

    duration = 2.4
    probs = native_track_probabilities(duration)
    segments = native_track_segments(duration)
    assert probs.shape == (int(round(duration / FRAME_DURATION)), NUM_SPEAKERS)
    assert float(probs[:, 0].min()) == 1.0
    assert float(probs[:, 1:].max()) == 0.0
    assert segments == [
        {"speaker": "speaker_0", "speaker_index": 0, "start": 0.0, "end": duration}
    ]
