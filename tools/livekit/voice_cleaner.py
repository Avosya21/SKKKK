"""CPU DeepFilterNet for live rooms. Not lab ASR. Not GPU."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")

# `python tools/livekit/voice_cleaner.py` puts this folder first on sys.path and
# shadows the pip `livekit` SDK.
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path[:] = [item for item in sys.path if Path(item).resolve() != _SCRIPT_DIR]

ROOT = Path(__file__).resolve().parents[2]
CLEANER_ID = "skkkk-dfn"
CLEANER_NAME = "очистка звука"
SAMPLE_RATE = 48000
QUEUE_MS = 200
FRAME_MS = 10
CHUNK_MS = 80
POLL_SEC = 2.0
DENOISE_KEY = "denoise"
DENOISE_TOPIC = "skkkk-denoise"
ATTEN_ENV = "MEETING_VOICE_ATTEN_LIM"
POST_ENV = "MEETING_VOICE_POST_FILTER"
ATTEN_DEFAULT = 20.0
INTERNAL_URL = os.environ.get("LIVEKIT_RTC_INTERNAL", "ws://127.0.0.1:7880")
HTTP_URL = os.environ.get("LIVEKIT_HTTP_INTERNAL", "http://127.0.0.1:7880")
_ATTEN = ATTEN_DEFAULT


def atten_lim_db(raw: str | None = None) -> float | None:
    if raw is None:
        raw = os.environ.get(ATTEN_ENV, str(int(ATTEN_DEFAULT)))
    text = raw.strip()
    if not text:
        return ATTEN_DEFAULT
    if text.lower() in {"0", "off", "full", "none"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return ATTEN_DEFAULT
    if value <= 0:
        return None
    return value


def post_filter_enabled(raw: str | None = None) -> bool:
    if raw is None:
        raw = os.environ.get(POST_ENV, "0")
    return raw.strip().lower() in {"1", "true", "on"}


def wants_denoise(metadata: str) -> bool:
    if not metadata:
        return True
    try:
        data = json.loads(metadata)
    except json.JSONDecodeError:
        return True
    return data.get(DENOISE_KEY, True) is not False


def parse_denoise_packet(data, topic: str = "") -> bool | None:
    if topic and topic != DENOISE_TOPIC:
        return None
    try:
        raw = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data or "")
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, AttributeError):
        return None
    if not isinstance(payload, dict) or DENOISE_KEY not in payload:
        return None
    return payload.get(DENOISE_KEY) is not False


def encode_identity(identity: str) -> str:
    return identity.replace(":", ".")


def clean_track_name(identity: str) -> str:
    return "clean-" + encode_identity(identity)


def _env(name: str) -> str:
    path = ROOT / ".env"
    if not path.exists():
        return os.environ.get(name, "")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return os.environ.get(name, "")


def _ensure_df_io_compat() -> None:
    """DeepFilterNet imports torchaudio.backend; torchaudio 2.9+ dropped it."""
    import sys
    import types

    try:
        import torchaudio  # noqa: F401
    except ImportError:
        pass
    if "torchaudio.backend.common" in sys.modules:
        return
    try:
        from torchaudio.backend.common import AudioMetaData  # noqa: F401
        return
    except Exception:
        pass
    common = types.ModuleType("torchaudio.backend.common")

    class AudioMetaData:
        def __init__(
            self,
            sample_rate=0,
            num_frames=0,
            num_channels=0,
            bits_per_sample=0,
            encoding="",
        ):
            self.sample_rate = sample_rate
            self.num_frames = num_frames
            self.num_channels = num_channels
            self.bits_per_sample = bits_per_sample
            self.encoding = encoding

    common.AudioMetaData = AudioMetaData
    backend = sys.modules.get("torchaudio.backend") or types.ModuleType("torchaudio.backend")
    backend.common = common
    sys.modules["torchaudio.backend"] = backend
    sys.modules["torchaudio.backend.common"] = common


def _load_filter():
    _ensure_df_io_compat()
    from df.enhance import enhance, init_df

    model, _state, _ = init_df(post_filter=post_filter_enabled(_env(POST_ENV) or "0"))
    return model, enhance


def _new_df_state():
    from df.model import ModelParams
    from libdf import DF

    params = ModelParams()
    return DF(
        sr=params.sr,
        fft_size=params.fft_size,
        hop_size=params.hop_size,
        nb_bands=params.nb_erb,
        min_nb_erb_freqs=params.min_nb_freqs,
    )


def _pcm16(frame) -> "object":
    import numpy as np

    return np.frombuffer(bytes(frame.data), dtype=np.int16).copy()


def _fit(samples, length: int):
    import numpy as np

    if len(samples) == length:
        return samples
    if len(samples) > length:
        return samples[:length]
    out = np.zeros(length, dtype=samples.dtype)
    out[: len(samples)] = samples
    return out


def _enhance(model, state, enhance, pcm, atten=None):
    import numpy as np
    import torch

    if atten is None:
        atten = _ATTEN
    audio = torch.from_numpy(pcm.astype(np.float32) / 32768.0).unsqueeze(0)
    reset = getattr(model, "reset_h0", None)
    if reset is not None:
        model.reset_h0 = lambda *args, **kwargs: None
    try:
        out = enhance(model, state, audio, pad=False, atten_lim_db=atten)
    finally:
        if reset is not None:
            model.reset_h0 = reset
    wave = out.detach().cpu().numpy().reshape(-1)
    wave = np.clip(wave, -1.0, 1.0)
    return _fit((wave * 32767.0).astype(np.int16), len(pcm))


def _is_human(identity: str) -> bool:
    return identity != CLEANER_ID


_REC_MOD = None


def _recordings():
    global _REC_MOD
    if _REC_MOD is None:
        import importlib.util

        path = ROOT / "app" / "app" / "services" / "meeting_recordings.py"
        spec = importlib.util.spec_from_file_location("skkkk_meeting_recordings", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _REC_MOD = mod
    return _REC_MOD


def _data_dir() -> Path:
    return Path(_env("DATA_DIR") or str(ROOT / "data"))


def _track_key(identity: str, source: str) -> str:
    return identity + "|" + source


def replace_async_task(tasks: dict, key: str, factory):
    old = tasks.get(key)
    if old is not None and not old.done():
        old.cancel()
    tasks[key] = factory()
    return tasks[key]


async def _record_track(track, writer, lock) -> None:
    from livekit import rtc

    stream = rtc.AudioStream(
        track, sample_rate=SAMPLE_RATE, num_channels=1, frame_size_ms=FRAME_MS
    )
    try:
        async for event in stream:
            pcm = _pcm16(event.frame)
            async with lock:
                writer.write(pcm.tobytes())
    finally:
        await stream.aclose()


def _bind_recorders(room_name: str, rec, data_dir: Path):
    if rec is None:
        def _noop(*_args, **_kwargs):
            return None

        async def _noop_close() -> None:
            return None

        return _noop, _noop, _noop_close, None

    writers: dict[str, object] = {}
    tasks: dict[str, asyncio.Task] = {}
    locks: dict[str, asyncio.Lock] = {}
    parts: dict[str, tuple[str, str]] = {}
    manifest = rec.empty_manifest(room_name)
    events = rec.MeetingEventLog(rec.room_dir(data_dir, room_name) / rec.EVENTS_NAME)

    def persist() -> None:
        dest = rec.room_dir(data_dir, room_name) / rec.MANIFEST_NAME
        rec.write_manifest(dest, manifest)

    def start_record(track, pub, participant) -> None:
        identity = participant.identity
        if not rec.should_record_identity(identity):
            return
        source = rec.source_from_publication(pub)
        key = _track_key(identity, source)
        writer = writers.get(key)
        if writer is None:
            try:
                path = rec.track_path(data_dir, room_name, identity, source)
                writer = rec.PcmWavWriter(
                    path,
                    sample_rate=SAMPLE_RATE,
                    on_gap=lambda sec, who=identity: events.append(
                        rec.EVENT_GAP, identity=who, sec=round(sec, 3)
                    ),
                )
                writers[key] = writer
                rec.upsert_track(
                    manifest,
                    identity=identity,
                    rel_path=f"{rec.TRACKS_DIRNAME}/{path.name}",
                    source=source,
                    sample_rate=SAMPLE_RATE,
                    started_epoch=time.time(),
                )
                persist()
                events.append(rec.EVENT_TRACK, identity=identity, event="start", source=source)
            except Exception as exc:
                print("voice_cleaner: record fail", room_name, identity, exc, flush=True)
                return
            print("voice_cleaner: record", room_name, identity, source, flush=True)
        parts[key] = (identity, source)
        locks.setdefault(key, asyncio.Lock())
        replace_async_task(
            tasks,
            key,
            lambda: asyncio.create_task(_record_track(track, writer, locks[key])),
        )

    def stop_record(pub, participant) -> None:
        key = _track_key(participant.identity, rec.source_from_publication(pub))
        task = tasks.pop(key, None)
        if task:
            task.cancel()

    async def close_all() -> None:
        pending = list(tasks.values())
        for task in pending:
            task.cancel()
        tasks.clear()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for key, writer in writers.items():
            writer.close()
            identity, source = parts.get(key, ("", ""))
            if identity:
                rec.set_track_frames(
                    manifest,
                    identity,
                    source,
                    writer.frames,
                    SAMPLE_RATE,
                    ended_epoch=time.time(),
                )
                events.append(
                    rec.EVENT_TRACK,
                    identity=identity,
                    event="end",
                    frames=writer.frames,
                    source=source,
                )
        if manifest.get("tracks"):
            persist()

    return start_record, stop_record, close_all, events


async def _unpublish_named(room, name: str) -> None:
    pubs = getattr(room.local_participant, "track_publications", None) or {}
    values = pubs.values() if hasattr(pubs, "values") else pubs
    for pub in list(values):
        pub_name = getattr(pub, "name", "") or getattr(getattr(pub, "track", None), "name", "")
        if pub_name != name:
            continue
        sid = getattr(pub, "sid", None)
        if not sid:
            continue
        try:
            await room.local_participant.unpublish_track(sid)
        except Exception:
            pass


async def _emit(source, rtc, pcm) -> None:
    hop = SAMPLE_RATE * FRAME_MS // 1000
    for start in range(0, len(pcm) - hop + 1, hop):
        piece = pcm[start : start + hop]
        await source.capture_frame(
            rtc.AudioFrame(
                data=piece.tobytes(),
                sample_rate=SAMPLE_RATE,
                num_channels=1,
                samples_per_channel=len(piece),
            )
        )


async def _pump(room, track, participant, model, state, enhance, lock, wanted) -> None:
    from livekit import rtc
    import numpy as np

    name = clean_track_name(participant.identity)
    await _unpublish_named(room, name)
    source = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=QUEUE_MS)
    local = rtc.LocalAudioTrack.create_audio_track(name, source)
    await room.local_participant.publish_track(
        local,
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
    )
    stream = rtc.AudioStream(
        track, sample_rate=SAMPLE_RATE, num_channels=1, frame_size_ms=FRAME_MS
    )
    need = SAMPLE_RATE * CHUNK_MS // 1000
    chunks: list = []
    try:
        async for event in stream:
            if not wanted():
                return
            chunks.append(_pcm16(event.frame))
            if sum(len(item) for item in chunks) < need:
                continue
            pcm = np.concatenate(chunks)
            chunks.clear()
            async with lock:
                clean = _enhance(model, state, enhance, pcm, _ATTEN)
            await _emit(source, rtc, clean)
    finally:
        await stream.aclose()
        try:
            await room.local_participant.unpublish_track(local.sid)
        except Exception:
            return


async def _join(url: str, token: str, model, enhance, lock, room_name: str) -> None:
    from livekit import rtc

    room = rtc.Room()
    pumps: dict[str, asyncio.Task] = {}
    rec_mod = _recordings()
    rec = rec_mod if rec_mod.recording_enabled(_env(rec_mod.RECORD_ENV) or "1") else None
    start_record, stop_record, close_records, events = _bind_recorders(room_name, rec, _data_dir())
    last_speakers: list[str] = []

    denoise_prefs: dict[str, bool] = {}

    def person_wants(participant) -> bool:
        identity = getattr(participant, "identity", "") or ""
        if identity in denoise_prefs:
            return denoise_prefs[identity]
        return wants_denoise(getattr(participant, "metadata", "") or "")

    def cancel(identity: str) -> None:
        task = pumps.pop(identity, None)
        if task:
            task.cancel()

    def start(track, participant) -> None:
        identity = participant.identity
        if not _is_human(identity):
            return
        if not person_wants(participant):
            cancel(identity)
            return
        cancel(identity)
        pumps[identity] = asyncio.create_task(
            _pump(
                room,
                track,
                participant,
                model,
                _new_df_state(),
                enhance,
                lock,
                lambda: person_wants(participant),
            )
        )

    def apply_denoise(participant) -> None:
        if person_wants(participant):
            pubs = getattr(participant, "track_publications", None) or {}
            values = pubs.values() if hasattr(pubs, "values") else pubs
            for pub in values:
                track = getattr(pub, "track", None)
                if track is not None and getattr(track, "kind", None) == rtc.TrackKind.KIND_AUDIO:
                    start(track, participant)
                    return
            return
        cancel(participant.identity)

    def _log_event(kind: str, **fields) -> None:
        if events is None:
            return
        identity = str(fields.get("identity") or "")
        if identity and not rec_mod.is_human_identity(identity):
            return
        events.append(kind, **fields)

    @room.on("participant_connected")
    def _on_join(participant) -> None:
        _log_event(rec_mod.EVENT_PARTICIPANT, identity=participant.identity, event="join")

    @room.on("participant_disconnected")
    def _on_leave(participant) -> None:
        _log_event(rec_mod.EVENT_PARTICIPANT, identity=participant.identity, event="leave")

    @room.on("active_speakers_changed")
    def _on_speakers(speakers) -> None:
        active = [
            person.identity
            for person in (speakers or [])
            if rec_mod.is_human_identity(getattr(person, "identity", "") or "")
        ]
        if not rec_mod.speakers_changed(last_speakers, active):
            return
        last_speakers[:] = active
        _log_event(rec_mod.EVENT_SPEAKERS, active=active)

    @room.on("track_muted")
    def _on_mute(participant, *_args) -> None:
        _log_event(rec_mod.EVENT_MUTE, identity=getattr(participant, "identity", ""), muted=True)

    @room.on("track_unmuted")
    def _on_unmute(participant, *_args) -> None:
        _log_event(rec_mod.EVENT_MUTE, identity=getattr(participant, "identity", ""), muted=False)

    @room.on("connection_quality_changed")
    def _on_quality(participant, quality) -> None:
        value = getattr(quality, "name", quality)
        _log_event(
            rec_mod.EVENT_QUALITY,
            identity=getattr(participant, "identity", ""),
            value=str(value).split(".")[-1].lower(),
        )

    @room.on("track_subscribed")
    def _on_track(track, _pub, participant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            start_record(track, _pub, participant)
            start(track, participant)

    @room.on("track_unsubscribed")
    def _on_unsub(track, _pub, participant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            stop_record(_pub, participant)
            cancel(participant.identity)

    @room.on("participant_metadata_changed")
    def _on_meta(participant, *_args) -> None:
        apply_denoise(participant)

    @room.on("data_received")
    def _on_data(packet) -> None:
        person = getattr(packet, "participant", None)
        identity = getattr(person, "identity", "") if person else ""
        parsed = parse_denoise_packet(
            getattr(packet, "data", b""), getattr(packet, "topic", "") or ""
        )
        if parsed is None or not identity:
            return
        denoise_prefs[identity] = parsed
        print("voice_cleaner: denoise", identity, parsed, flush=True)
        apply_denoise(person)

    await room.connect(url, token)
    print("voice_cleaner: joined", room.name, flush=True)
    try:
        while True:
            if room.connection_state != rtc.ConnectionState.CONN_CONNECTED:
                return
            await asyncio.sleep(POLL_SEC)
    finally:
        for task in list(pumps.values()):
            task.cancel()
        await close_records()
        await room.disconnect()


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def livekit_jwt(payload: dict, secret: str, kid: str) -> str:
    import hashlib
    import hmac
    import json

    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(sig)}"


def _tokens(api_key: str, api_secret: str, room_name: str = "") -> tuple[str, str]:
    now = int(time.time())
    listed = livekit_jwt(
        {"iss": api_key, "nbf": now, "exp": now + 3600, "video": {"roomList": True}},
        api_secret,
        api_key,
    )
    joined = ""
    if room_name:
        joined = livekit_jwt(
            {
                "iss": api_key,
                "sub": CLEANER_ID,
                "nbf": now,
                "exp": now + 21600,
                "name": CLEANER_NAME,
                "video": {
                    "roomJoin": True,
                    "room": room_name,
                    "canSubscribe": True,
                    "canPublish": True,
                    "hidden": True,
                },
            },
            api_secret,
            api_key,
        )
    return listed, joined


def _list_rooms(list_jwt: str) -> list[str]:
    import urllib.request

    req = urllib.request.Request(
        HTTP_URL.rstrip("/") + "/twirp/livekit.RoomService/ListRooms",
        data=b"{}",
        headers={"Authorization": "Bearer " + list_jwt, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    names = []
    for row in payload.get("rooms") or []:
        name = row.get("name") or ""
        if name.startswith("skkkk-"):
            names.append(name)
    return names


async def _loop() -> None:
    key = _env("LIVEKIT_API_KEY")
    secret = _env("LIVEKIT_API_SECRET")
    if not key or not secret:
        print("voice_cleaner: no LiveKit keys", file=sys.stderr)
        return
    global _ATTEN
    _ATTEN = atten_lim_db(_env(ATTEN_ENV) or None)
    model, enhance = _load_filter()
    lock = asyncio.Lock()
    print("voice_cleaner: DeepFilterNet CPU ready", "atten", _ATTEN, flush=True)
    active: set[str] = set()
    while True:
        list_jwt, _ = _tokens(key, secret)
        try:
            rooms = _list_rooms(list_jwt)
        except Exception as exc:
            print("voice_cleaner: list", exc, file=sys.stderr)
            rooms = []
        for name in rooms:
            if name in active:
                continue
            _, join_jwt = _tokens(key, secret, name)
            active.add(name)
            asyncio.create_task(_watch(name, join_jwt, model, enhance, lock, active))
        await asyncio.sleep(POLL_SEC)


async def _watch(name: str, token: str, model, enhance, lock, active: set[str]) -> None:
    try:
        await _join(INTERNAL_URL, token, model, enhance, lock, name)
    except Exception as exc:
        print("voice_cleaner:", name, exc, file=sys.stderr)
    finally:
        active.discard(name)


def main() -> int:
    if os.environ.get("MEETING_VOICE_SERVER", "1").strip() in {"0", "false", "off"}:
        return 0
    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
