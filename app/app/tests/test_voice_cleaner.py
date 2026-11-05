"""Рекордер голоса tools/livekit/voice_cleaner.py: шумодав теперь в браузере, рекордер пишет сырую дорожку primaryanalitics-raw."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "tools" / "livekit" / "voice_cleaner.py"


def _voice_cleaner():
    spec = importlib.util.spec_from_file_location("voice_cleaner_mod", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_join_stays_until_livekit_disconnects():
    text = SRC.read_text(encoding="utf-8")
    assert "voice_cleaner: joined" in text
    assert "empty_leave_ticks" not in text


def test_recorder_does_not_clean_or_publish():
    # Серверный шумодав убран: никакого DeepFilterNet/torch, никаких clean-* и переключателя по данным
    text = SRC.read_text(encoding="utf-8").split('"""', 2)[2]  # код без исторической справки в docstring
    for gone in ("deepfilternet", "import torch", "df.enhance", "clean-", "publish_track", "primaryanalitics-denoise", "data_received"):
        assert gone not in text.lower(), gone
    install = (ROOT / "scripts" / "install_meeting_voice.py").read_text(encoding="utf-8")
    assert "torch" not in install and '"livekit"' in install


def test_raw_track_name_matches_browser():
    vc = _voice_cleaner()
    js = (ROOT / "app" / "app" / "static" / "meeting_room.js").read_text(encoding="utf-8")
    assert f'const RAW_TRACK_NAME = "{vc.RAW_TRACK_NAME}";' in js


def _pub(name="", source=2, track=None):
    return SimpleNamespace(name=name, source=source, track=track)


def _person(identity, *pubs):
    return SimpleNamespace(identity=identity, track_publications={str(i): p for i, p in enumerate(pubs)})


def test_voice_pub_to_record_prefers_raw():
    vc = _voice_cleaner()
    mic, raw, screen = _pub("", 2), _pub(vc.RAW_TRACK_NAME, 0), _pub("", 4)
    desktop = _person("member:1", mic, raw, screen)
    phone = _person("member:2", mic)
    assert vc.voice_pub_to_record(raw, desktop, "microphone", "microphone") is True
    assert vc.voice_pub_to_record(mic, desktop, "microphone", "microphone") is False  # очищенная — слушателям
    assert vc.voice_pub_to_record(screen, desktop, "screen", "microphone") is True  # звук экрана пишем как раньше
    assert vc.voice_pub_to_record(mic, phone, "microphone", "microphone") is True  # без сырой — основная


def test_recorder_switches_to_raw_and_back(tmp_path, monkeypatch):
    """Основная пришла первой → пишем её; пришла сырая → та же WAV продолжается с сырой; сняли основную — запись
    сырой не трогаем; сырая пропала → возвращаемся к основной."""
    vc = _voice_cleaner()
    rec = vc._recordings()
    playing: list[str] = []

    async def fake_record(track, writer, lock):
        playing.append(track.sid)
        await asyncio.Event().wait()

    monkeypatch.setattr(vc, "_record_track", fake_record)

    async def scenario():
        start, stop, close, _events = vc._bind_recorders("primaryanalitics-test", rec, tmp_path)
        mic_track, raw_track = SimpleNamespace(sid="TR_mic"), SimpleNamespace(sid="TR_raw")
        mic = _pub("", 2, mic_track)
        person = _person("member:7", mic)
        start(mic_track, mic, person)
        await asyncio.sleep(0)
        raw = _pub(vc.RAW_TRACK_NAME, 0, raw_track)
        person.track_publications["raw"] = raw
        start(raw_track, raw, person)
        await asyncio.sleep(0)
        start(mic_track, mic, person)  # повторная подписка на основную при живой сырой — игнор
        stop(mic_track, mic, person)  # снятие основной не рвёт запись сырой
        await asyncio.sleep(0)
        del person.track_publications["raw"]
        stop(raw_track, raw, person)
        await asyncio.sleep(0)
        await close()

    asyncio.run(scenario())
    assert playing == ["TR_mic", "TR_raw", "TR_mic"]
    wavs = list((tmp_path / "meetings" / "primaryanalitics-test" / "tracks").glob("*.wav"))
    assert [p.name for p in wavs] == ["member-7.wav"]  # одна дорожка голоса на человека


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


def test_recorder_token_listens_only_and_hidden():
    # Рекордер ничего не публикует, поэтому он hidden, как и запись экрана: нет пустой плитки и строки в списке
    from jose import jwt

    vc = _voice_cleaner()
    _, joined = vc._tokens("devkey", "secret", "primaryanalitics-room")
    grant = jwt.decode(joined, "secret", algorithms=["HS256"])["video"]
    assert grant["canSubscribe"] is True and grant["canPublish"] is False
    assert grant["hidden"] is True


def test_records_manifest_events_and_speakers():
    text = SRC.read_text(encoding="utf-8")
    assert "meeting_recordings.py" in text
    assert "write_manifest_merged" in text
    assert "voice_cleaner: record" in text
    assert "EVENTS_NAME" in text
    assert "active_speakers_changed" in text


def test_cleaner_raises_own_priority():
    # с обычным приоритетом на занятой машине цикл событий замирал на 0,4–1 с
    text = SRC.read_text(encoding="utf-8")
    main = text.split("def main() -> int:")[1]
    assert "_raise_priority()" in main
    assert "above_normal = 0x00008000" in text
    assert "kernel32.GetCurrentProcess.restype = ctypes.c_void_p" in text
