"""Рекордер голоса комнаты: пишет каждого человека в свою WAV-дорожку для двойного слуха. Не ASR, не GPU.

Исторически здесь был серверный шумодав (DeepFilterNet, участник `skkkk-dfn` публиковал очищенные `clean-*`).
С 2026-10-05 шумодав работает в браузере говорящего (DFN3, см. DECISIONS «Живой звук комнаты»), а сюда браузер
отдаёт отдельную сырую дорожку `skkkk-raw` — её и пишем. Имя файла и участника оставлены прежними: по ним работают
защита от второй копии, `restart.ps1`, журналы и проверки запуска.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

# `python tools/livekit/voice_cleaner.py` puts this folder first on sys.path and
# shadows the pip `livekit` SDK.
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path[:] = [item for item in sys.path if Path(item).resolve() != _SCRIPT_DIR]

ROOT = Path(__file__).resolve().parents[2]
CLEANER_ID = "skkkk-dfn"
CLEANER_NAME = "запись голоса"
SAMPLE_RATE = 48000
FRAME_MS = 10
POLL_SEC = 2.0
# Сырая дорожка микрофона (до шумодава браузера), то же имя — RAW_TRACK_NAME в app/app/static/meeting_room.js
RAW_TRACK_NAME = "skkkk-raw"
INTERNAL_URL = os.environ.get("LIVEKIT_RTC_INTERNAL", "ws://127.0.0.1:7880")
HTTP_URL = os.environ.get("LIVEKIT_HTTP_INTERNAL", "http://127.0.0.1:7880")


def _env(name: str) -> str:
    path = ROOT / ".env"
    if not path.exists():
        return os.environ.get(name, "")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return os.environ.get(name, "")


def _is_human(identity: str) -> bool:
    return identity != CLEANER_ID


def _publication_name(pub) -> str:
    return getattr(pub, "name", "") or getattr(getattr(pub, "track", None), "name", "") or ""


def is_raw_publication(pub) -> bool:
    return _publication_name(pub) == RAW_TRACK_NAME


def _publications(participant) -> list:
    pubs = getattr(participant, "track_publications", None) or {}
    return list(pubs.values() if hasattr(pubs, "values") else pubs)


def has_raw_publication(participant) -> bool:
    return any(is_raw_publication(pub) for pub in _publications(participant))


def voice_pub_to_record(pub, participant, source: str, microphone: str) -> bool:
    """Какую дорожку голоса писать: сырую, если человек её публикует (компьютер с DFN3 в браузере), иначе основную
    (телефон, DFN3 не завёлся, старый клиент). Очищенная основная дорожка рядом с сырой — для слушателей, не для записи."""
    if source != microphone or is_raw_publication(pub):
        return True
    return not has_raw_publication(participant)


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
            async with lock:
                writer.write(bytes(event.frame.data))
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
        rec.write_manifest_merged(dest, manifest)

    def start_record(track, pub, participant) -> None:
        identity = participant.identity
        if not rec.should_record_identity(identity):
            return
        source = rec.source_from_publication(pub)
        if not voice_pub_to_record(pub, participant, source, rec.SOURCE_MICROPHONE):
            return
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
                    # рекордер перезапустили посреди встречи: продолжаем существующую дорожку, а не затираем её
                    resume_started_epoch=rec.existing_track_started(data_dir, room_name, identity, source),
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
        # сырая дорожка пришла после основной: та же WAV продолжается с сырой (запись по стенным часам, без сдвига)
        task = replace_async_task(
            tasks,
            key,
            lambda: asyncio.create_task(_record_track(track, writer, locks[key])),
        )
        task.skkkk_track = getattr(track, "sid", "") or ""
        print("voice_cleaner: voice", room_name, identity, source, "raw" if is_raw_publication(pub) else "main", flush=True)

    def stop_record(track, pub, participant) -> None:
        key = _track_key(participant.identity, rec.source_from_publication(pub))
        task = tasks.get(key)
        sid = getattr(track, "sid", "") or ""
        if task is None or (sid and getattr(task, "skkkk_track", "") != sid):
            return  # снимают не ту дорожку, что сейчас пишется (основную, пока пишем сырую)
        tasks.pop(key, None)
        task.cancel()
        if not is_raw_publication(pub):
            return
        # сырая пропала, а голос идёт (переподключение браузера, отказ DFN3): пишем основную
        for other in _publications(participant):
            other_track = getattr(other, "track", None)
            if other is pub or other_track is None or is_raw_publication(other):
                continue
            if rec.source_from_publication(other) == rec.SOURCE_MICROPHONE:
                start_record(other_track, other, participant)
                return

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


async def _join(url: str, token: str, room_name: str, member_check=None) -> None:
    from livekit import rtc

    room = rtc.Room()
    rec_mod = _recordings()
    rec = rec_mod if rec_mod.recording_enabled(_env(rec_mod.RECORD_ENV) or "1") else None
    start_record, stop_record, close_records, events = _bind_recorders(room_name, rec, _data_dir())
    last_speakers: list[str] = []

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

    # сырая дорожка глушится вместе с микрофоном — событие пишем один раз, по основной
    @room.on("track_muted")
    def _on_mute(participant, pub=None, *_args) -> None:
        if pub is not None and is_raw_publication(pub):
            return
        _log_event(rec_mod.EVENT_MUTE, identity=getattr(participant, "identity", ""), muted=True)

    @room.on("track_unmuted")
    def _on_unmute(participant, pub=None, *_args) -> None:
        if pub is not None and is_raw_publication(pub):
            return
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
    def _on_track(track, pub, participant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO and _is_human(participant.identity):
            start_record(track, pub, participant)

    @room.on("track_unsubscribed")
    def _on_unsub(track, pub, participant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            stop_record(track, pub, participant)

    left = asyncio.Event()

    @room.on("disconnected")
    def _on_disconnected(*_args) -> None:
        left.set()

    await room.connect(url, token)
    print("voice_cleaner: joined", room.name, flush=True)
    watch = rec_mod.MemberWatch()
    try:
        while True:
            if left.is_set() or room.connection_state != rtc.ConnectionState.CONN_CONNECTED:
                return
            if member_check is not None and watch.due() and watch.lost(await asyncio.to_thread(member_check)):
                print("voice_cleaner: server lost us in", room_name, "- rejoin", flush=True)
                return
            await asyncio.sleep(POLL_SEC)
    finally:
        await close_records()
        try:
            await asyncio.wait_for(room.disconnect(), timeout=5.0)
        except BaseException:  # noqa: BLE001 — SDK мог застрять; комнату всё равно отпускаем, цикл зайдёт заново
            pass


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def livekit_jwt(payload: dict, secret: str, kid: str) -> str:
    import hashlib
    import hmac

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
                    "canPublish": False,
                    "canPublishData": False,
                    # рекордер только слушает: hidden — его не видно в комнате и нет пустой плитки
                    "hidden": True,
                },
            },
            api_secret,
            api_key,
        )
    return listed, joined


def _member_check(api_key: str, api_secret: str, room_name: str):
    def check() -> bool | None:
        now = int(time.time())
        admin = livekit_jwt(
            {"iss": api_key, "nbf": now, "exp": now + 300, "video": {"roomAdmin": True, "room": room_name}},
            api_secret,
            api_key,
        )
        return _recordings().room_member_state(HTTP_URL, admin, room_name, CLEANER_ID)

    return check


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
    print("voice_cleaner: voice recorder ready", flush=True)
    active: set[str] = set()
    backoff = _recordings().JoinBackoff()
    while True:
        list_jwt, _ = _tokens(key, secret)
        try:
            rooms = _list_rooms(list_jwt)
        except Exception as exc:
            print("voice_cleaner: list", exc, file=sys.stderr)
            rooms = []
        backoff.forget_missing(rooms)
        live = _recordings().live_room_names(_data_dir())
        for name in rooms:
            if name in active or not backoff.allowed(name):
                continue
            if live is not None and name not in live:
                continue  # комната не идущей встречи (могла воскреснуть от старого токена) — не заходим и не пишем
            _, join_jwt = _tokens(key, secret, name)
            active.add(name)
            backoff.started(name)
            asyncio.create_task(_watch(name, join_jwt, active, backoff, _member_check(key, secret, name)))
        await asyncio.sleep(POLL_SEC)


async def _watch(name: str, token: str, active: set[str], backoff=None, member_check=None) -> None:
    try:
        await _join(INTERNAL_URL, token, name, member_check)
    except Exception as exc:
        print("voice_cleaner:", name, exc, file=sys.stderr)
    finally:
        active.discard(name)
        if backoff is not None:
            backoff.finished(name)


def _raise_priority() -> str:
    """Запись идёт в реальном времени: с обычным приоритетом на занятой машине цикл событий замирал на 0,4–1 с,
    очередь кадров переполнялась. Повышенный приоритет держит запись ровной."""
    if os.name != "nt":
        return "skip"
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    above_normal = 0x00008000
    return "above_normal" if kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), above_normal) else "failed"


def main() -> int:
    if os.environ.get("MEETING_VOICE_SERVER", "1").strip() in {"0", "false", "off"}:
        return 0
    # одна копия на машину: вторая с тем же именем участника выбивала бы первую из комнаты
    instance = _recordings().acquire_single_instance(_data_dir() / "locks" / "voice_cleaner.instance")
    if instance is None:
        print("voice_cleaner: уже запущен, эта копия выходит", flush=True)
        return 0
    print("voice_cleaner: priority", _raise_priority(), flush=True)
    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        return 0
    finally:
        instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
