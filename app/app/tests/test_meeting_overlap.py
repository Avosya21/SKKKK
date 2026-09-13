from app.app.services.meeting_merge import apply_leaks, find_leaks, word_text
from app.app.services.meeting_overlap import (
    LEAK_BY_DIAR,
    LEAK_BY_RMS,
    LEAK_BY_UNRESOLVED,
    SPEAKER_UNKNOWN,
    STATUS_CONFLICT,
    assign_one_mic,
    refine_leaks,
)


def _pair(name, start):
    return [
        {"word": "один", "final": "один", "start": start, "end": start + 0.3, "speaker": name},
        {"word": "два", "final": "два", "start": start + 0.3, "end": start + 0.6, "speaker": name},
    ]


def test_exclusive_rank_still_drops_loser():
    events = [{"t": 34.2, "kind": "speakers", "active": ["guest:A"]}]
    leaks = find_leaks([_pair("guest:A", 34.2), _pair("guest:B", 34.25)], events)
    assert leaks[0]["by"] == "rank"
    assert leaks[0]["speaker"] == "guest:B"
    cleaned = apply_leaks([_pair("guest:A", 34.2), _pair("guest:B", 34.25)], leaks)
    assert [word_text(row) for row in cleaned[1]] == []


def test_same_room_without_proof_is_conflict():
    groups = [_pair("A", 10.0), _pair("B", 10.05)]
    leaks = refine_leaks(groups, find_leaks(groups, []), events=[], rms={})
    assert leaks[0]["by"] == LEAK_BY_UNRESOLVED
    assert leaks[0]["status"] == STATUS_CONFLICT
    cleaned = apply_leaks(groups, leaks)
    assert [word_text(row) for row in cleaned[0]]
    assert [word_text(row) for row in cleaned[1]]


def test_same_room_rms_breaks_tie():
    groups = [_pair("A", 10.0), _pair("B", 10.05)]
    leaks = refine_leaks(groups, find_leaks(groups), rms={"A": 0.2, "B": 0.02})
    assert leaks[0]["by"] == LEAK_BY_RMS
    assert leaks[0]["from_speaker"] == "A"
    cleaned = apply_leaks(groups, leaks)
    assert [word_text(row) for row in cleaned[1]] == []


def test_same_room_rms_callable_uses_window():
    groups = [_pair("A", 10.0), _pair("B", 10.05)]

    def rms(name, start, end):
        assert end > start
        return 0.3 if name == "A" else 0.01

    leaks = refine_leaks(groups, find_leaks(groups), rms=rms)
    assert leaks[0]["by"] == LEAK_BY_RMS
    assert leaks[0]["from_speaker"] == "A"


def test_same_room_diar_only_on_window():
    groups = [_pair("A", 10.0), _pair("B", 10.05)]
    seen = []

    def diar(left, right, start, end):
        seen.append((left, right, start, end))
        return left

    leaks = refine_leaks(groups, find_leaks(groups), rms={"A": 0.1, "B": 0.1}, diar=diar)
    assert seen
    assert leaks[0]["by"] == LEAK_BY_DIAR
    assert leaks[0]["from_speaker"] == "A"


def test_one_mic_aba_not_aaa():
    words = [
        {"word": "раз", "final": "раз", "start": 0.0, "end": 0.4, "speaker": "A"},
        {"word": "два", "final": "два", "start": 1.0, "end": 1.4, "speaker": "A"},
        {"word": "три", "final": "три", "start": 2.0, "end": 2.4, "speaker": "A"},
    ]
    segments = [
        {"start": 0.0, "end": 0.7, "cluster": "c1"},
        {"start": 0.7, "end": 1.6, "cluster": "c2"},
        {"start": 1.6, "end": 3.0, "cluster": "c1"},
    ]
    out = assign_one_mic(words, segments, {"c1": "A", "c2": "B"})
    assert [row["speaker"] for row in out] == ["A", "B", "A"]


def test_one_mic_unmapped_cluster_is_unknown():
    words = [{"word": "раз", "final": "раз", "start": 0.0, "end": 0.4, "speaker": "A"}]
    out = assign_one_mic(words, [{"start": 0.0, "end": 1.0, "cluster": "c9"}], {})
    assert out[0]["speaker"] == SPEAKER_UNKNOWN
    assert out[0]["status"] == STATUS_CONFLICT
