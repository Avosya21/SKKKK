"""Синтетический контрольный созвон вместо живой записи A–I."""

from app.app.services.meeting_analyze import meeting_duration_sec
from app.app.services.meeting_checks import STATUS_PASS, STATUS_WARN, run_checks
from app.app.services.meeting_merge import (
    apply_leaks,
    attach_turn_ids,
    find_leaks,
    merge_track_replicas,
    word_text,
)
from app.app.services.meeting_overlap import (
    LEAK_BY_UNRESOLVED,
    SPEAKER_UNKNOWN,
    assign_one_mic,
    refine_leaks,
)
from app.app.services.meeting_recordings import speaker_group_key as group_key


def _words(name, start, tokens):
    rows = []
    when = start
    for index, token in enumerate(tokens):
        rows.append(
            {
                "i": index,
                "word": token,
                "final": token,
                "start": when,
                "end": when + 0.4,
                "speaker": name,
            }
        )
        when += 0.5
    return rows


def test_control_call_synthetic_phases():
    one = _words("A", 1.0, ["я", "номер", "один"])
    two = _words("B", 3.0, ["я", "номер", "два"])
    three = _words("C", 5.0, ["я", "номер", "три"])
    replicas = merge_track_replicas(one, two, three)
    assert [row["speaker"] for row in replicas] == ["A", "B", "C"]

    overlap_a = _words("A", 10.0, ["один", "два", "три"])
    overlap_b = _words("B", 10.4, ["раз", "два"])
    overlapped = merge_track_replicas(overlap_a, overlap_b)
    assert overlapped[0]["text"] == "один два три"
    assert overlapped[1]["text"] == "раз два"

    meeting = {
        "duration_sec": 40.0,
        "meeting_started_epoch": 1000.0,
        "meeting_ended_epoch": 1040.0,
        "tracks": [
            {
                "identity": "member:28",
                "started_epoch": 1001.0,
                "ended_epoch": 1031.0,
                "frames": 48000 * 30,
                "sample_rate": 48000,
                "offset_sec": 1.0,
                "duration_sec": 30.0,
            }
        ],
    }
    events = [
        {"t": 1010.0, "kind": "gap", "identity": "C", "sec": 30.0},
        {"t": 1010.2, "kind": "mute", "identity": "C", "muted": True},
        {"t": 1001.0, "t_rel": 1.0, "kind": "speakers", "active": ["A"]},
    ]
    checks = {row["id"]: row for row in run_checks(meeting, events, one)}
    assert checks["gaps"]["status"] == STATUS_PASS
    assert checks["clock_integrity"]["status"] in {STATUS_PASS, STATUS_WARN}

    assert group_key("member:28") == group_key("member:28")
    assert group_key("guest:aaa") != group_key("guest:bbb")

    leak_src = _words("A", 20.0, ["один", "два"])
    leak_dst = _words("B", 20.05, ["один", "два"])
    leaks = find_leaks([leak_src, leak_dst], [{"t": 20.0, "kind": "speakers", "active": ["A"]}])
    assert leaks[0]["by"] == "rank"
    assert [word_text(row) for row in apply_leaks([leak_src, leak_dst], leaks)[1]] == []

    same_room = refine_leaks([leak_src, leak_dst], find_leaks([leak_src, leak_dst]))
    assert same_room[0]["by"] == LEAK_BY_UNRESOLVED

    one_mic = assign_one_mic(
        _words("A", 30.0, ["раз", "два", "три"]),
        [
            {"start": 30.0, "end": 30.6, "cluster": "c1"},
            {"start": 30.6, "end": 31.2, "cluster": "c2"},
            {"start": 31.2, "end": 32.0, "cluster": "c1"},
        ],
        {"c1": "A", "c2": "B"},
    )
    assert [row["speaker"] for row in one_mic] == ["A", "B", "A"]
    assert SPEAKER_UNKNOWN not in [row["speaker"] for row in one_mic]

    screen = {"source": "screen", "speaker": "external_or_unknown", "identity": "member:1"}
    assert screen["speaker"] == "external_or_unknown"
    assert meeting_duration_sec([{"offset_sec": 1.0, "duration_sec": 10.0}, {"offset_sec": 0.0, "duration_sec": 8.0}]) == 11.0

    turns = attach_turn_ids(replicas, "art_ctrl")
    assert all(row.get("source_span_id", "").startswith("turn:art_ctrl:") for row in turns)
