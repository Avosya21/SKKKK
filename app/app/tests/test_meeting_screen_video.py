from pathlib import Path

from app.app.services import meeting_recordings as rec
from app.app.services.meeting_analyze import analyzable_tracks, screen_tracks, screen_video_tracks


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_room(tmp_path: Path, room: str) -> Path:
    dest = rec.room_dir(tmp_path, room)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / rec.TRACKS_DIRNAME).mkdir(exist_ok=True)
    return dest


def test_screen_video_is_not_asr_and_not_screen_audio(tmp_path: Path):
    room = "skkkk-screenvid01"
    dest = _seed_room(tmp_path, room)
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
    rec.upsert_screen_video(
        manifest,
        identity="member:1",
        rel_path="tracks/member-1-screen_video-TRone.mp4",
        track_sid="TR_one",
        started_epoch=2.0,
        fps=1,
        width=640,
        height=360,
    )
    rec.set_screen_video_frames(manifest, "member:1", "TR_one", 90, 1, ended_epoch=92.0)
    rec.write_manifest(dest / rec.MANIFEST_NAME, manifest)
    writer = rec.PcmWavWriter(dest / "tracks" / "member-1.wav")
    writer.write(b"\x00\x00" * 480)
    writer.close()
    (dest / "tracks" / "member-1-screen.wav").write_bytes(b"RIFFfake" + b"\x00" * 40)
    (dest / "tracks" / "member-1-screen_video-TRone.mp4").write_bytes(b"ftyp" + b"\x00" * 60)
    mics = analyzable_tracks(tmp_path, room)
    assert [row["identity"] for row in mics] == ["member:1"]
    assert screen_tracks(tmp_path, room)[0]["source"] == rec.SOURCE_SCREEN
    videos = screen_video_tracks(tmp_path, room)
    assert len(videos) == 1
    assert videos[0]["source"] == rec.SOURCE_SCREEN_VIDEO
    assert videos[0]["track_sid"] == "TR_one"
    assert videos[0]["frames"] == 90


def test_parent_mix_uses_silent_mic_when_nobody_spoke(tmp_path: Path):
    from app.app.services.pipeline_meeting import parent_mix_inputs

    wav = tmp_path / "silent.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 40)
    prepared = [
        {"source": "microphone", "wav16": str(wav), "offset_sec": 1.5, "silent": True},
        {"source": "screen_video", "wav16": "", "offset_sec": 0},
    ]
    rows = parent_mix_inputs(prepared, [])
    assert rows == [(wav, 1.5)]


def test_analyze_keeps_screen_video_on_publisher(client, manager_token, tmp_data):
    from app.app.config import get_settings
    from app.app.services.storage import artifact_dir

    headers = _auth(manager_token)
    pid = client.post("/projects", json={"name": "ScreenVid"}, headers=headers).json()["id"]
    created = client.post(f"/projects/{pid}/meetings", headers=headers).json()
    mid = created["id"]
    client.post(f"/projects/{pid}/meetings/{mid}/start", headers=headers)
    member = client.post(f"/projects/{pid}/meetings/{mid}/join", headers=headers).json()
    client.post(f"/projects/{pid}/meetings/{mid}/end", headers=headers)
    room = client.get(f"/projects/{pid}/meetings/{mid}", headers=headers).json()["room_name"]
    dest = rec.room_dir(get_settings().data_dir, room)
    wav = dest / rec.TRACKS_DIRNAME / "member-mic.wav"
    writer = rec.PcmWavWriter(wav)
    writer.write(b"\x00\x00" * 480)
    writer.close()
    mp4 = dest / rec.TRACKS_DIRNAME / "member-screen_video-TRone.mp4"
    mp4.parent.mkdir(parents=True, exist_ok=True)
    mp4.write_bytes(b"ftyp" + b"\x00" * 80)
    manifest = rec.empty_manifest(room)
    rec.upsert_track(
        manifest,
        identity=member["identity"],
        rel_path=f"{rec.TRACKS_DIRNAME}/{wav.name}",
        source=rec.SOURCE_MICROPHONE,
        sample_rate=rec.SAMPLE_RATE,
        started_epoch=1000.0,
    )
    rec.set_track_frames(manifest, member["identity"], rec.SOURCE_MICROPHONE, 480, rec.SAMPLE_RATE)
    rec.upsert_screen_video(
        manifest,
        identity=member["identity"],
        rel_path=f"{rec.TRACKS_DIRNAME}/{mp4.name}",
        track_sid="TR_one",
        started_epoch=1001.0,
        fps=1,
        width=640,
        height=360,
    )
    rec.set_screen_video_frames(manifest, member["identity"], "TR_one", 90, 1, ended_epoch=1091.0)
    rec.write_manifest(dest / rec.MANIFEST_NAME, manifest)
    body = client.post(f"/projects/{pid}/meetings/{mid}/analyze", headers=headers).json()
    assert len(body["analyze_jobs"]) == 1
    assert body["analyze_jobs"][0]["identity"] == member["identity"]
    parent = body["analyze_jobs"][0]["artifact_id"]
    payload = __import__("json").loads(
        (artifact_dir(get_settings(), pid, parent) / "meeting.json").read_text(encoding="utf-8")
    )
    videos = [row for row in payload["tracks"] if row["source"] == rec.SOURCE_SCREEN_VIDEO]
    assert len(videos) == 1
    assert videos[0]["speaker"] == member["display_name"]
    assert videos[0]["speaker"] != "external_or_unknown"
    assert videos[0]["track_sid"] == "TR_one"
    assert payload["duration_sec"] >= 90
