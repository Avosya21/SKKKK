from pathlib import Path

from app.app.services import meeting_recordings as rec
from app.app.services.meeting_analyze import analyzable_tracks, screen_tracks


def test_screen_audio_is_not_mixed_into_speech(tmp_path: Path):
    room = "skkkk-screen01"
    manifest = rec.empty_manifest(room)
    rec.upsert_track(
        manifest,
        identity="member:1",
        rel_path="tracks/member-1.wav",
        source=rec.SOURCE_MICROPHONE,
        sample_rate=rec.SAMPLE_RATE,
        started_epoch=1.0,
    )
    rec.upsert_track(
        manifest,
        identity="member:1",
        rel_path="tracks/member-1-screen.wav",
        source=rec.SOURCE_SCREEN,
        sample_rate=rec.SAMPLE_RATE,
        started_epoch=1.0,
    )
    dest = rec.room_dir(tmp_path, room)
    rec.write_manifest(dest / rec.MANIFEST_NAME, manifest)
    writer = rec.PcmWavWriter(dest / "tracks" / "member-1.wav")
    writer.write(b"\x00\x00" * 480)
    writer.close()
    (dest / "tracks" / "member-1-screen.wav").write_bytes(b"RIFFfake" + b"\x00" * 40)
    mics = analyzable_tracks(tmp_path, room)
    screens = screen_tracks(tmp_path, room)
    assert [row["identity"] for row in mics] == ["member:1"]
    assert screens[0]["source"] == rec.SOURCE_SCREEN
    assert rec.safe_track_stem("member:1", rec.SOURCE_SCREEN) == "member-1-screen"
