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
