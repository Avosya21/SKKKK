from __future__ import annotations

import array
import math
import wave
from pathlib import Path

from app.app.services import meeting_recordings as rec

ROOM_A = "skkkk-aaa111"
ROOM_B = "skkkk-bbb222"
MEMBER = "member:28"
GUEST = "guest:3a78ff"
FRAME_FRAMES = 480
SILENCE = b"\x00\x00" * FRAME_FRAMES
TRACK_START = 1000.5


def test_recording_enabled_defaults_on():
    assert rec.recording_enabled("") is True
    assert rec.recording_enabled("1") is True
    assert rec.recording_enabled("0") is False


def test_skips_cleaner_and_keeps_humans():
    assert rec.is_human_identity(MEMBER) is True
    assert rec.is_human_identity(GUEST) is True
    assert rec.is_human_identity(rec.CLEANER_ID) is False
    assert rec.is_human_identity("skkkk-rec") is False


def test_denoise_opt_out_does_not_change_who_is_recorded():
    assert rec.should_record_identity(MEMBER) is True
    assert rec.should_record_identity(rec.CLEANER_ID) is False


def test_wav_writer_roundtrip(tmp_path: Path):
    path = tmp_path / "t.wav"
    writer = rec.PcmWavWriter(path, sample_rate=rec.SAMPLE_RATE)
    writer.write(SILENCE)
    writer.close()
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == rec.CHANNELS
        assert handle.getsampwidth() == rec.SAMPLE_WIDTH
        assert handle.getframerate() == rec.SAMPLE_RATE
        assert handle.getnframes() == 480


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_wav_writer_pads_mute_gap(tmp_path: Path):
    """Молчащий микрофон не должен сжимать шкалу: пропуск добивается тишиной."""
    clock = FakeClock()
    writer = rec.PcmWavWriter(tmp_path / "gap.wav", clock=clock)
    writer.write(SILENCE)
    clock.now = 1.0
    writer.write(SILENCE)
    writer.close()
    assert writer.frames == rec.SAMPLE_RATE + FRAME_FRAMES


def test_wav_writer_does_not_stretch_without_gap(tmp_path: Path):
    clock = FakeClock()
    writer = rec.PcmWavWriter(tmp_path / "tight.wav", clock=clock)
    writer.write(SILENCE)
    writer.write(SILENCE)
    writer.close()
    assert writer.frames == 2 * FRAME_FRAMES


def test_manifest_keeps_track_start(tmp_path: Path):
    manifest = rec.empty_manifest(ROOM_A)
    rec.upsert_track(
        manifest,
        identity=MEMBER,
        rel_path="tracks/member-28.wav",
        source=rec.SOURCE_MICROPHONE,
        sample_rate=rec.SAMPLE_RATE,
        started_epoch=TRACK_START,
    )
    rec.upsert_track(
        manifest,
        identity=MEMBER,
        rel_path="tracks/member-28.wav",
        source=rec.SOURCE_MICROPHONE,
        sample_rate=rec.SAMPLE_RATE,
        started_epoch=TRACK_START + 200.0,
    )
    assert manifest["tracks"][0]["started_epoch"] == TRACK_START


def test_tracks_of_one_room_share_one_manifest(tmp_path: Path):
    manifest = rec.empty_manifest(ROOM_A)
    rec.upsert_track(
        manifest,
        identity=MEMBER,
        rel_path="tracks/member-28.wav",
        source=rec.SOURCE_MICROPHONE,
        sample_rate=rec.SAMPLE_RATE,
        started_epoch=TRACK_START,
    )
    rec.upsert_track(
        manifest,
        identity=GUEST,
        rel_path="tracks/guest-3a78ff.wav",
        source=rec.SOURCE_MICROPHONE,
        sample_rate=rec.SAMPLE_RATE,
        started_epoch=TRACK_START,
    )
    rec.upsert_track(
        manifest,
        identity=MEMBER,
        rel_path="tracks/member-28.wav",
        source=rec.SOURCE_MICROPHONE,
        sample_rate=rec.SAMPLE_RATE,
        started_epoch=TRACK_START,
    )
    dest = rec.room_dir(tmp_path, ROOM_A) / rec.MANIFEST_NAME
    rec.write_manifest(dest, manifest)
    tracks = rec.tracks_for_meeting(tmp_path, ROOM_A)
    assert [row["identity"] for row in tracks] == [MEMBER, GUEST]
    assert {row["path"] for row in tracks} == {
        "tracks/member-28.wav",
        "tracks/guest-3a78ff.wav",
    }
    assert rec.tracks_for_meeting(tmp_path, ROOM_B) == []
    assert rec.tracks_linked_to_room(manifest, ROOM_B) == []


def test_screen_audio_is_a_separate_file():
    assert rec.safe_track_stem(MEMBER) == "member-28"
    assert rec.safe_track_stem(MEMBER, rec.SOURCE_SCREEN) == "member-28-screen"


def test_writer_reports_gap(tmp_path: Path):
    clock = FakeClock()
    seen = []
    writer = rec.PcmWavWriter(tmp_path / "gap-event.wav", clock=clock, on_gap=seen.append)
    writer.write(SILENCE)
    clock.now = 1.0
    writer.write(SILENCE)
    writer.close()
    assert len(seen) == 1
    assert seen[0] >= rec.GAP_PAD_SEC


def test_event_log_append(tmp_path: Path):
    path = tmp_path / rec.EVENTS_NAME
    log = rec.MeetingEventLog(path, clock=lambda: 1750000000.123)
    log.append(rec.EVENT_SPEAKERS, active=["member:28"])
    rows = rec.read_events(path)
    assert rows[0]["kind"] == rec.EVENT_SPEAKERS
    assert rows[0]["active"] == ["member:28"]
    assert rows[0]["t"] == 1750000000.123


def test_event_log_recovers_from_bad_line(tmp_path: Path):
    path = tmp_path / rec.EVENTS_NAME
    path.write_text('{"t": 1, "kind": "mute"}\nnot-json\n{"t": 2, "kind": "gap"}\n', encoding="utf-8")
    rows = rec.read_events(path)
    assert [row["kind"] for row in rows] == ["mute", "gap"]


def test_speakers_changed():
    assert rec.speakers_changed(["a"], ["a"]) is False
    assert rec.speakers_changed(["a"], ["a", "b"]) is True


def test_has_speech_skips_silence_and_keeps_tone(tmp_path: Path):
    silent = tmp_path / "silent.wav"
    spoken = tmp_path / "speech.wav"
    _write_wav(silent, [0] * rec.SAMPLE_RATE * 3)
    tone = [
        int(12000 * math.sin(2 * math.pi * 440 * i / rec.SAMPLE_RATE))
        for i in range(rec.SAMPLE_RATE * 3)
    ]
    _write_wav(spoken, tone)
    assert rec.has_speech(silent) is False
    assert rec.has_speech(spoken) is True
    assert rec.window_rms(silent, 0.0, 1.0) < rec.window_rms(spoken, 0.0, 1.0)
    assert rec.window_rms(tmp_path / "missing.wav", 0.0, 1.0) == 0.0


def test_speaker_group_key_keeps_guest_identities_apart():
    assert rec.speaker_group_key("member:28") == "member:28"
    assert rec.speaker_group_key("guest:aaa") != rec.speaker_group_key("guest:bbb")


def test_remove_room_recordings(tmp_path: Path):
    dest = rec.room_dir(tmp_path, ROOM_A)
    dest.mkdir(parents=True)
    (dest / rec.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    rec.remove_room_recordings(tmp_path, ROOM_A)
    assert dest.exists() is False


def _write_wav(path: Path, samples: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rec.SAMPLE_RATE)
        handle.writeframes(array.array("h", samples).tobytes())
