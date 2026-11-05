from __future__ import annotations

import array
import math
import wave
from pathlib import Path
from types import SimpleNamespace

from app.app.services import meeting_recordings as rec

ROOM_A = "primaryanalitics-aaa111"
ROOM_B = "primaryanalitics-bbb222"
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
    assert rec.is_human_identity("primaryanalitics-rec") is False


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


def test_source_from_publication_reads_livekit_ints():
    assert rec.source_from_publication(SimpleNamespace(source=3)) == rec.SOURCE_SCREEN
    assert rec.source_from_publication(SimpleNamespace(source=4)) == rec.SOURCE_SCREEN
    assert rec.source_from_publication(SimpleNamespace(source=2)) == rec.SOURCE_MICROPHONE
    assert rec.source_from_publication(SimpleNamespace(source="SOURCE_SCREENSHARE")) == rec.SOURCE_SCREEN


def test_screen_audio_is_a_separate_file():
    assert rec.safe_track_stem(MEMBER) == "member-28"
    assert rec.safe_track_stem(MEMBER, rec.SOURCE_SCREEN) == "member-28-screen"
    assert rec.safe_track_stem(MEMBER, rec.SOURCE_SCREEN_VIDEO) == "member-28-screen_video"


def test_safe_sid_strips_and_caps():
    assert rec.safe_sid("TR_abc-12") == "TR_abc-12"
    assert rec.safe_sid("TR/xxx yyy!") == "TRxxxyyy"
    assert rec.safe_sid("") == "sid"
    assert len(rec.safe_sid("A" * 40)) == rec.SID_SAFE_LEN


def test_screen_video_relpath_keeps_audio_apart():
    rel = rec.screen_video_relpath(MEMBER, "TR_abc12")
    assert rel == "tracks/member-28-screen_video-TR_abc12.mp4"
    assert rec.safe_track_stem(MEMBER, rec.SOURCE_SCREEN) + ".wav" != Path(rel).name


def test_upsert_screen_video_one_sid_one_row():
    manifest = rec.empty_manifest(ROOM_A)
    rec.upsert_screen_video(
        manifest,
        identity=MEMBER,
        rel_path="tracks/member-28-screen_video-TR_one.mp4",
        track_sid="TR_one",
        started_epoch=TRACK_START,
        fps=1,
        width=1280,
        height=720,
    )
    rec.upsert_screen_video(
        manifest,
        identity=MEMBER,
        rel_path="tracks/member-28-screen_video-TR_one.mp4",
        track_sid="TR_one",
        started_epoch=TRACK_START + 50,
        fps=1,
        width=1280,
        height=720,
    )
    rec.upsert_screen_video(
        manifest,
        identity=MEMBER,
        rel_path="tracks/member-28-screen_video-TR_two.mp4",
        track_sid="TR_two",
        started_epoch=TRACK_START + 80,
        fps=1,
        width=800,
        height=600,
    )
    videos = [row for row in manifest["tracks"] if row["source"] == rec.SOURCE_SCREEN_VIDEO]
    assert len(videos) == 2
    assert videos[0]["started_epoch"] == TRACK_START
    assert videos[0]["track_sid"] == "TR_one"
    assert videos[1]["track_sid"] == "TR_two"
    rec.set_screen_video_frames(manifest, MEMBER, "TR_one", 40, 1, ended_epoch=TRACK_START + 40)
    assert videos[0]["frames"] == 40
    assert videos[0]["duration_sec"] == 40.0
    assert videos[0]["ended_epoch"] == TRACK_START + 40


def test_screen_recording_follows_meeting_record(monkeypatch):
    assert rec.screen_recording_enabled("1", "1") is True
    assert rec.screen_recording_enabled("0", "1") is False
    assert rec.screen_recording_enabled("1", "0") is False
    assert rec.screen_fps("1") == 1.0
    assert rec.screen_max_width("") == rec.SCREEN_MAX_W_DEFAULT
    assert rec.screen_height("") == 1080
    assert rec.screen_bitrate("5M") == 5_000_000
    assert rec.screen_canvas() == (1920, 1080)


def test_write_manifest_merged_keeps_audio_and_video(tmp_path: Path):
    dest = rec.room_dir(tmp_path, ROOM_A) / rec.MANIFEST_NAME
    audio = rec.empty_manifest(ROOM_A)
    rec.upsert_track(
        audio,
        identity=MEMBER,
        rel_path="tracks/member-28.wav",
        source=rec.SOURCE_MICROPHONE,
        sample_rate=rec.SAMPLE_RATE,
        started_epoch=TRACK_START,
    )
    rec.write_manifest(dest, audio)
    video = rec.empty_manifest(ROOM_A)
    rec.upsert_screen_video(
        video,
        identity=MEMBER,
        rel_path="tracks/member-28-screen_video-TRone.mp4",
        track_sid="TR_one",
        started_epoch=TRACK_START,
    )
    rec.write_manifest_merged(dest, video)
    tracks = rec.read_manifest(dest)["tracks"]
    sources = {row["source"] for row in tracks}
    assert sources == {rec.SOURCE_MICROPHONE, rec.SOURCE_SCREEN_VIDEO}


def test_remove_room_recordings_deletes_screen_mp4(tmp_path: Path):
    dest = rec.room_dir(tmp_path, ROOM_A)
    tracks = dest / rec.TRACKS_DIRNAME
    tracks.mkdir(parents=True)
    (dest / rec.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    mp4 = tracks / "member-28-screen_video-TRone.mp4"
    mp4.write_bytes(b"ftyp" + b"\x00" * 60)
    rec.remove_room_recordings(tmp_path, ROOM_A)
    assert dest.exists() is False
    assert mp4.exists() is False


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


def test_writer_never_truncates_existing_track_and_resumes(tmp_path):
    """Регрессия: перезапуск рекордера посреди встречи обнулял дорожку (wave.open «wb»): потеряны 5 минут голоса."""
    path = tmp_path / "member.wav"
    t = [0.0]
    first = rec.PcmWavWriter(path, sample_rate=8000, clock=lambda: t[0])
    for _ in range(10):
        first.write(b"\x01\x00" * 800)  # 0.1 с звука
        t[0] += 0.1
    frames_before = first.frames
    first.close()
    size_before = path.stat().st_size
    # «перезапуск»: новый писатель открывает тот же файл, 5 секунд стенных часов прошли без записи
    started = 1_000_000.0
    t2 = [100.0]
    second = rec.PcmWavWriter(path, sample_rate=8000, clock=lambda: t2[0], resume_started_epoch=started, wall_clock=lambda: started + frames_before / 8000 + 5.0)
    assert second.resumed and second.frames >= frames_before + int(4.9 * 8000)  # старое сохранено, пропуск заполнен тишиной
    second.write(b"\x02\x00" * 800)
    second.close()
    import wave

    with wave.open(str(path), "rb") as w:
        assert w.getnframes() == second.frames and w.getnframes() > frames_before
        data = w.readframes(w.getnframes())
    assert data[: frames_before * 2] == b"\x01\x00" * frames_before  # начало дорожки не тронуто
    assert path.stat().st_size > size_before


def test_writer_header_is_readable_after_crash(tmp_path):
    """Процесс убит без close(): заголовок обновлялся по ходу, файл читается и содержит почти всё записанное."""
    import wave

    path = tmp_path / "crash.wav"
    t = [0.0]
    w = rec.PcmWavWriter(path, sample_rate=8000, clock=lambda: t[0])
    for _ in range(50):
        w.write(b"\x03\x00" * 800)
        t[0] += 0.1  # 5 с; заголовок обновляется каждые 2 с
    with wave.open(str(path), "rb") as r:  # без w.close(): «упавший» процесс
        assert r.getnframes() >= 3 * 8000


def test_foreign_file_is_set_aside_not_overwritten(tmp_path):
    path = tmp_path / "x.wav"
    path.write_bytes(b"not a wav file at all" * 10)
    w = rec.PcmWavWriter(path)
    w.close()
    assert any(p.name.startswith("x.unreadable-") for p in tmp_path.iterdir())


def test_single_instance_lock_blocks_second_copy(tmp_path):
    """Вторая копия рекордера с тем же именем участника выбивает первую из комнаты LiveKit: блокировка не даёт её запустить."""
    lock = tmp_path / "locks" / "voice_cleaner.instance"
    first = rec.acquire_single_instance(lock)
    assert first is not None
    assert rec.acquire_single_instance(lock) is None  # уже занято
    first.close()
    again = rec.acquire_single_instance(lock)  # освободилось: следующий запуск проходит
    assert again is not None
    again.close()


def test_single_instance_lock_is_released_when_process_dies(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    lock = tmp_path / "x.instance"
    code = (
        "import sys, importlib.util; from pathlib import Path\n"
        f"spec = importlib.util.spec_from_file_location('r', r'{Path(rec.__file__)}'); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"h = m.acquire_single_instance(Path(r'{lock}')); print('got' if h else 'busy'); sys.stdout.flush()\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60).stdout.strip()
    assert out == "got"  # процесс завершился, замок не «протух»
    held = rec.acquire_single_instance(lock)
    assert held is not None
    held.close()


def test_join_backoff_backs_off_failing_rooms_and_forgets_success():
    t = [0.0]
    b = rec.JoinBackoff(base=30, cap=100, min_ok=20, clock=lambda: t[0])
    assert b.allowed("r")
    b.started("r")
    t[0] += 2  # подключение продержалось 2 с: неудача
    b.finished("r")
    assert not b.allowed("r")
    t[0] += 31
    assert b.allowed("r")
    b.started("r"); t[0] += 1; b.finished("r")  # вторая неудача: пауза 60 с
    t[0] += 45
    assert not b.allowed("r")
    t[0] += 20
    assert b.allowed("r")
    b.started("r"); t[0] += 300; b.finished("r")  # долгое подключение: счётчик сброшен
    assert b.allowed("r")
    for _ in range(6):  # пауза не растёт бесконечно
        b.started("r"); t[0] += 1; b.finished("r"); t[0] += 1000
    b.started("r"); t[0] += 1; b.finished("r")
    assert not b.allowed("r") and b._next["r"] - t[0] <= 100
    b.forget_missing([])  # комната исчезла из LiveKit
    assert b.allowed("r")


def test_member_watch_leaves_after_two_server_misses():
    now = [0.0]
    watch = rec.MemberWatch(every=10.0, misses=2, clock=lambda: now[0])
    assert not watch.due()
    now[0] = 10.0
    assert watch.due() and not watch.due()
    assert not watch.lost(None)  # сервер не ответил — не повод выходить
    assert not watch.lost(False)
    assert not watch.lost(True)  # снова в комнате — счёт сначала
    assert not watch.lost(False)
    assert watch.lost(False)


def test_room_member_state_asks_livekit_server():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["path"], seen["auth"], seen["room"] = self.path, self.headers["Authorization"], body["room"]
            if body["room"] == "primaryanalitics-gone":
                self.send_response(404)
                self.end_headers()
                return
            out = json.dumps({"participants": [{"identity": "guest:1"}, {"identity": "primaryanalitics-dfn"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert rec.room_member_state(url, "jwt", "primaryanalitics-a", "primaryanalitics-dfn") is True
        assert seen == {"path": "/twirp/livekit.RoomService/ListParticipants", "auth": "Bearer jwt", "room": "primaryanalitics-a"}
        assert rec.room_member_state(url, "jwt", "primaryanalitics-a", "primaryanalitics-rec") is False
        assert rec.room_member_state(url, "jwt", "primaryanalitics-gone", "primaryanalitics-dfn") is False
    finally:
        server.shutdown()
    assert rec.room_member_state("http://127.0.0.1:9", "jwt", "primaryanalitics-a", "primaryanalitics-dfn", timeout=1) is None


def test_bots_watch_server_membership():
    root = Path(__file__).resolve().parents[3] / "tools" / "livekit"
    for name, bot in (("voice_cleaner.py", "CLEANER_ID"), ("screen_recorder.py", "RECORDER_ID")):
        text = (root / name).read_text(encoding="utf-8")
        assert "rec_mod.MemberWatch()" in text
        assert '@room.on("disconnected")' in text
        assert "asyncio.wait_for(room.disconnect(), timeout=5.0)" in text
        assert f"room_member_state(HTTP_URL, admin, room_name, {bot})" in text
        assert '"roomAdmin": True' in text
