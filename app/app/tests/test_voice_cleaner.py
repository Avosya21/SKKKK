import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _voice_cleaner():
    path = ROOT / "tools" / "livekit" / "voice_cleaner.py"
    spec = importlib.util.spec_from_file_location("voice_cleaner_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_join_stays_until_livekit_disconnects():
    text = (ROOT / "tools" / "livekit" / "voice_cleaner.py").read_text(encoding="utf-8")
    assert "voice_cleaner: joined" in text
    assert "empty_leave_ticks" not in text


def test_install_script_pins_torchaudio():
    text = (ROOT / "scripts" / "install_meeting_voice.py").read_text(encoding="utf-8")
    assert "torchaudio" in text


def test_df_io_compat_exposes_audio_metadata():
    vc = _voice_cleaner()
    vc._ensure_df_io_compat()
    from torchaudio.backend.common import AudioMetaData

    assert AudioMetaData(sample_rate=48000).sample_rate == 48000


def test_wants_denoise_defaults_on():
    vc = _voice_cleaner()
    assert vc.wants_denoise("") is True
    assert vc.wants_denoise('{"denoise": true}') is True
    assert vc.wants_denoise('{"denoise": false}') is False
    assert vc.wants_denoise("not-json") is True


def test_atten_lim_db():
    vc = _voice_cleaner()
    assert vc.atten_lim_db("20") == 20.0
    assert vc.atten_lim_db("12") == 12.0
    assert vc.atten_lim_db("0") is None
    assert vc.atten_lim_db("full") is None
    assert vc.post_filter_enabled("1") is True
    assert vc.post_filter_enabled("0") is False


def test_parse_denoise_packet():
    vc = _voice_cleaner()
    assert vc.parse_denoise_packet(b'{"denoise": false}', vc.DENOISE_TOPIC) is False
    assert vc.parse_denoise_packet(b'{"denoise": true}', vc.DENOISE_TOPIC) is True
    assert vc.parse_denoise_packet(b'{"denoise": false}', "other") is None
    assert vc.parse_denoise_packet(b"nope", vc.DENOISE_TOPIC) is None


def test_clean_track_name():
    vc = _voice_cleaner()
    assert vc.clean_track_name("member:28") == "clean-member.28"
    assert vc.clean_track_name("guest:abc") == "clean-guest.abc"


def test_live_enhance_pads_stft_and_resets_model():
    import inspect

    vc = _voice_cleaner()
    src = inspect.getsource(vc._enhance)
    assert "enhance(model, state, audio, pad=True" in src
    assert "reset_h0 = lambda" not in src


def test_records_raw_tracks_before_denoise_pump():
    text = (ROOT / "tools" / "livekit" / "voice_cleaner.py").read_text(encoding="utf-8")
    on_track = text.split("def _on_track")[1].split("def _on_unsub")[0]
    assert on_track.find("start_record") < on_track.find("start(track")
    assert "meeting_recordings.py" in text
    assert "write_manifest_merged" in text
    assert "voice_cleaner: denoise" in text
    assert "voice_cleaner: record" in text
    assert "EVENTS_NAME" in text
    assert "active_speakers_changed" in text
    assert '"atten"' in text


def test_replace_async_task_cancels_previous():
    vc = _voice_cleaner()
    cancelled = []

    class FakeTask:
        def __init__(self, done=False):
            self._done = done

        def done(self):
            return self._done

        def cancel(self):
            cancelled.append(self)

    tasks = {"member:28|microphone": FakeTask()}
    nxt = FakeTask(done=True)
    vc.replace_async_task(tasks, "member:28|microphone", lambda: nxt)
    assert cancelled
    assert tasks["member:28|microphone"] is nxt


def test_livekit_jwt_roundtrip():
    vc = _voice_cleaner()
    token = vc.livekit_jwt({"iss": "devkey", "video": {"roomList": True}}, "secret", "devkey")
    from jose import jwt

    payload = jwt.decode(token, "secret", algorithms=["HS256"])
    assert payload["iss"] == "devkey"
    assert payload["video"]["roomList"] is True


def test_cleaner_token_is_visible_recorder_stays_hidden():
    # hidden-участника LiveKit прячет вместе с дорожками: очищенный clean-* не доходил бы до слушателей
    from jose import jwt

    vc = _voice_cleaner()
    _, joined = vc._tokens("devkey", "secret", "skkkk-room")
    grant = jwt.decode(joined, "secret", algorithms=["HS256"])["video"]
    assert grant["canPublish"] is True and grant["canSubscribe"] is True
    assert not grant.get("hidden")
    recorder = (ROOT / "tools" / "livekit" / "screen_recorder.py").read_text(encoding="utf-8")
    assert '"hidden": True' in recorder


def test_max_tracks_env():
    vc = _voice_cleaner()
    assert vc.max_tracks(None) == vc.MAX_TRACKS_DEFAULT
    assert vc.max_tracks("") == vc.MAX_TRACKS_DEFAULT
    assert vc.max_tracks("12") == 12
    assert vc.max_tracks("0") == 1
    assert vc.max_tracks("много") == vc.MAX_TRACKS_DEFAULT


def test_pump_uses_live_filter_and_cap():
    text = (ROOT / "tools" / "livekit" / "voice_cleaner.py").read_text(encoding="utf-8")
    start = text.split("    def start(track, participant)")[1].split("    def apply_denoise")[0]
    assert "_LiveFilter(model, enhance)" in start
    assert "len(_BUSY) >= _MAX_TRACKS" in start
    assert "state.process(pcm, _ATTEN)" in text
    assert "analysis(x[:n].reshape(1, -1), False)" in text
    assert "synthesis(np.ascontiguousarray(enh.numpy()), False)" in text


_LIVE_CHECK = r"""
import sys
import numpy as np
import torch
sys.path.insert(0, sys.argv[1])
import voice_cleaner as vc
vc._ensure_df_io_compat()
model, enhance = vc._load_filter()
rng = np.random.default_rng(3)
sr = 48000
t = np.arange(sr * 3) / sr
x = (0.3 * np.sin(2 * np.pi * 220 * t) * (np.sin(2 * np.pi * 1.3 * t) > 0) + 0.05 * rng.standard_normal(len(t))).astype(np.float32)
pcm = (x * 32767).astype(np.int16)
ref = enhance(model, vc._new_df_state(), torch.from_numpy(pcm.astype(np.float32) / 32768.0).unsqueeze(0), pad=False, atten_lim_db=20.0)
ref = ref.numpy().reshape(-1)
live = vc._LiveFilter(model, enhance)
assert live.live, "streaming GRU hooks not installed"
out = np.concatenate([live.process(pcm[i:i + 3840], 20.0) for i in range(0, len(pcm) - 3839, 3840)]).astype(np.float32) / 32768.0
n = min(len(out), len(ref))
print("frames", n, "maxdiff", float(np.max(np.abs(out[:n] - np.clip(ref[:n], -1, 1)))))
"""


def test_live_filter_matches_whole_recording():
    """Потоковый DFN3 по 80 мс = обработка всей записи разом (с точностью до int16). Нужен venvs/voice."""
    import subprocess

    import pytest

    py = ROOT / "venvs" / "voice" / "Scripts" / "python.exe"
    if not py.is_file():
        pytest.skip("нет venvs/voice")
    done = subprocess.run(
        [str(py), "-c", _LIVE_CHECK, str(ROOT / "tools" / "livekit")],
        capture_output=True,
        text=True,
        timeout=180,
        env={**__import__("os").environ, "CUDA_VISIBLE_DEVICES": "-1"},
    )
    line = [row for row in done.stdout.splitlines() if row.startswith("frames")]
    assert done.returncode == 0 and line, done.stderr[-2000:]
    frames, maxdiff = int(line[0].split()[1]), float(line[0].split()[3])
    assert frames >= 3 * 48000 - 3840
    assert maxdiff < 2e-3



def test_backlog_drops_only_accumulated_input():
    vc = _voice_cleaner()
    assert vc.frames_to_drop(0) == 0
    assert vc.frames_to_drop(7) == 0  # норма: 8 дорожек ждут замок
    assert vc.frames_to_drop(vc.BACKLOG_FRAMES) == 0
    assert vc.frames_to_drop(200) == 200 - vc.BACKLOG_KEEP  # цикл застрял на 2 с

    from collections import deque

    class Ring:
        def __init__(self, items):
            self._queue = deque(items)

    class Stream:
        def __init__(self, items):
            self._queue = Ring(items)

    stream = Stream(["f"] * 20 + [None])
    assert vc._queued_frames(stream) == 20
    assert vc._drop_queued(stream, 18) == 18
    assert list(stream._queue._queue) == ["f", "f", None]
    assert vc._drop_queued(stream, 10) == 2  # маркер конца потока не выбрасываем
    assert list(stream._queue._queue) == [None]
    assert vc._queued_frames(object()) == 0


def test_pump_bounds_input_queue():
    text = (ROOT / "tools" / "livekit" / "voice_cleaner.py").read_text(encoding="utf-8")
    pump = text.split("async def _pump(")[1].split("async def _join(")[0]
    assert "capacity=STREAM_CAP_FRAMES" in pump
    assert "_drop_queued(stream, frames_to_drop(queued))" in pump
    assert "_warm_up(model, enhance)" in text


def test_cleaner_slots_released_on_cancel_and_room_exit():
    vc = _voice_cleaner()
    first, second, other = object(), object(), object()
    vc._BUSY.clear()
    vc._BUSY.update({"skkkk-a|guest:1": first, "skkkk-a|guest:2": second, "skkkk-b|guest:3": other})
    vc.release_slot("skkkk-a|guest:1", object())  # чужая (уже заменённая) задача место не трогает
    assert "skkkk-a|guest:1" in vc._BUSY
    vc.release_slot("skkkk-a|guest:1", first)
    assert "skkkk-a|guest:1" not in vc._BUSY
    vc.release_room_slots("skkkk-a")
    assert list(vc._BUSY) == ["skkkk-b|guest:3"]
    vc._BUSY.clear()
    text = (ROOT / "tools" / "livekit" / "voice_cleaner.py").read_text(encoding="utf-8")
    assert "release_room_slots(room_name)" in text
    assert "asyncio.wait_for(stream.aclose(), timeout=2.0)" in text


def test_pump_survives_double_toggle_and_stuck_publish():
    text = (ROOT / "tools" / "livekit" / "voice_cleaner.py").read_text(encoding="utf-8")
    start = text.split("    def start(track, participant)")[1].split("    def apply_denoise")[0]
    assert 'getattr(running, "skkkk_track", "") == track_sid' in start
    assert "task.skkkk_track = track_sid" in start
    pump = text.split("async def _pump(")[1].split("async def _join(")[0]
    assert "if stream is not None:" in pump
    assert "publish_track" not in pump and "unpublish_track" not in pump  # публикацией владеет комната, не задача


def test_one_clean_track_per_person_for_whole_stay():
    # Переключатель шумодава и переподключение не публикуют clean-* заново: две публикации одного человека
    # у слушателей звучали дважды со сдвигом (эхо, задвоение).
    text = (ROOT / "tools" / "livekit" / "voice_cleaner.py").read_text(encoding="utf-8")
    join = text.split("async def _join(")[1].split("def _b64url")[0]
    assert "async def get_out(identity: str):" in join and "async def drop_out(identity: str)" in join
    assert "if identity in outs:" in join
    assert "timeout=PUBLISH_TIMEOUT_S" in join
    assert "asyncio.ensure_future(drop_out(participant.identity))" in join  # ушёл человек — снимаем его clean-*
    assert "asyncio.ensure_future(drop_out(identity))" in join  # задача упала — тоже
    pump = text.split("async def _pump(")[1].split("async def _join(")[0]
    assert "await get_out(participant.identity)" in pump and "source.clear_queue()" in pump


def test_only_voice_is_cleaned_and_new_pump_survives_old_track():
    text = (ROOT / "tools" / "livekit" / "voice_cleaner.py").read_text(encoding="utf-8")
    assert "return source != rtc.TrackSource.SOURCE_SCREENSHARE_AUDIO" in text
    on_track = text.split("def _on_track")[1].split("def _on_unsub")[0]
    assert 'is_voice_source(getattr(_pub, "source", None))' in on_track
    on_unsub = text.split("def _on_unsub")[1].split("@room.on")[0]
    assert 'getattr(running, "skkkk_track", "") == (getattr(track, "sid", "") or "")' in on_unsub


def test_cleaner_raises_own_priority():
    # с обычным приоритетом на занятой машине шумодав замирал на 0,4–1 с и выбрасывал ~2 с звука в минуту
    text = (ROOT / "tools" / "livekit" / "voice_cleaner.py").read_text(encoding="utf-8")
    main = text.split("def main() -> int:")[1]
    assert '_raise_priority()' in main
    assert "above_normal = 0x00008000" in text
    assert "kernel32.GetCurrentProcess.restype = ctypes.c_void_p" in text
