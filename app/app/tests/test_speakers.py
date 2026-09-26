import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "asr"))

from speakers import (  # noqa: E402
    Turn,
    apply_map,
    apply_merged_windows,
    canonical_speaker,
    carve_text_by_speakers,
    drop_contained_islands,
    drop_minor_speakers,
    flatten_overlaps,
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
    )
    turns = parse_timecode_lines(text)
    assert len(turns) == 2
    assert canonical_speaker(turns[0].speaker) == "Спикер №1"


def test_smooth_aba_island():
    turns = [
        Turn(0.0, 10.0, "Спикер №1", "раз"),
        Turn(10.0, 10.4, "Спикер №2", "угу"),
        Turn(10.4, 20.0, "Спикер №1", "два"),
    ]
    out = smooth_islands(turns, min_duration=0.8)
    assert len(out) == 1
    assert out[0].speaker == "Спикер №1"


def test_overlay_assigns_by_overlap():
    speakers = [Turn(0.0, 5.0, "Спикер №1"), Turn(5.0, 10.0, "Спикер №2")]
    words = [Turn(0.2, 4.8, "", "алёна говорит"), Turn(5.1, 9.0, "", "влад отвечает")]
    out = overlay_text(words, speakers)
    assert [t.speaker for t in out] == ["Спикер №1", "Спикер №2"]


def test_rename_merge_and_talk_time():
    turns = [
        Turn(0.0, 4.0, "Спикер №1", "а"),
        Turn(4.0, 6.0, "Спикер №2", "б"),
        Turn(6.0, 10.0, "Спикер №1", "в"),
    ]
    named = apply_map(turns, {"Спикер №1": "Алёна", "Спикер №2": "Пётр"})
    merged = merge_speakers(named, "Пётр", "Алёна")
    assert {t.speaker for t in merged} == {"Алёна"}
    stats = talk_time(turns)
    assert stats[0]["seconds"] == 8.0
