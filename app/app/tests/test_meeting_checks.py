from app.app.services.meeting_checks import (
    STATUS_FAIL,
    STATUS_NO_DATA,
    STATUS_PASS,
    STATUS_WARN,
    run_checks,
)


def _by_id(rows):
    return {row["id"]: row for row in rows}


def test_track_span_fail_and_pass():
    meeting = {
        "duration_sec": 10.0,
        "tracks": [{"identity": "a", "offset_sec": 0.0, "duration_sec": 9.0}],
    }
    assert _by_id(run_checks(meeting))["track_span"]["status"] == STATUS_PASS
    meeting["tracks"][0]["duration_sec"] = 20.0
    assert _by_id(run_checks(meeting))["track_span"]["status"] == STATUS_FAIL


def test_track_frames_thresholds():
    meeting = {
        "duration_sec": 2.0,
        "tracks": [
            {
                "identity": "a",
                "started_epoch": 100.0,
                "ended_epoch": 101.0,
                "frames": 48000,
                "sample_rate": 48000,
                "offset_sec": 0,
                "duration_sec": 1.0,
            }
        ],
    }
    assert _by_id(run_checks(meeting))["track_frames"]["status"] == STATUS_PASS
    meeting["tracks"][0]["ended_epoch"] = 102.0
    assert _by_id(run_checks(meeting))["track_frames"]["status"] == STATUS_FAIL


def test_gaps_need_mute():
    events = [{"t": 10.0, "kind": "gap", "identity": "a", "sec": 2.0}]
    assert _by_id(run_checks({"tracks": []}, events))["gaps"]["status"] == STATUS_WARN
    events.append({"t": 10.2, "kind": "mute", "identity": "a", "muted": True})
    assert _by_id(run_checks({"tracks": []}, events))["gaps"]["status"] == STATUS_PASS


def test_leaks_without_trace_fail():
    ok = [{"speaker": "B", "from_speaker": "A", "start": 1.0, "end": 2.0, "text": "один два", "by": "rank"}]
    assert _by_id(run_checks({"tracks": []}, leaks=ok))["leaks"]["status"] == STATUS_WARN
    bad = [{"speaker": "B"}]
    assert _by_id(run_checks({"tracks": []}, leaks=bad))["leaks"]["status"] == STATUS_FAIL


def test_old_meeting_started_epoch_is_no_data():
    assert _by_id(run_checks({"tracks": [{}]}))["no_started_epoch"]["status"] == STATUS_NO_DATA
    meeting = {"meeting_started_epoch": 1.0, "tracks": [{"identity": "a"}]}
    assert _by_id(run_checks(meeting))["no_started_epoch"]["status"] == STATUS_FAIL


def test_speaker_sync_stats():
    meeting = {"duration_sec": 5.0, "tracks": []}
    events = [{"t": 1.0, "t_rel": 1.0, "kind": "speakers", "active": ["A"]}]
    tape = [{"speaker": "A", "start": 1.1, "word": "раз"}]
    row = _by_id(run_checks(meeting, events, tape))["speaker_sync"]
    assert row["status"] == STATUS_PASS
    assert "median" in row


def test_clock_integrity_no_data_without_bounds():
    assert _by_id(run_checks({"tracks": []}))["clock_integrity"]["status"] == STATUS_NO_DATA
