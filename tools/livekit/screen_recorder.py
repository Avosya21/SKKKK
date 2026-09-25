"""CPU screen-share recorder for live rooms. Not DFN. Not ASR. Not camera."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path[:] = [item for item in sys.path if Path(item).resolve() != _SCRIPT_DIR]

ROOT = Path(__file__).resolve().parents[2]
RECORDER_ID = "skkkk-rec"
RECORDER_NAME = "запись экрана"
POLL_SEC = 2.0
HEARTBEAT_SEC = 2.5
INTERNAL_URL = os.environ.get("LIVEKIT_RTC_INTERNAL", "ws://127.0.0.1:7880")
HTTP_URL = os.environ.get("LIVEKIT_HTTP_INTERNAL", "http://127.0.0.1:7880")
SCREEN_RECORD_ENV = "MEETING_SCREEN_RECORD"
FFMPEG_TIMEOUT_SEC = 8

_REC_MOD = None


def main_disabled(raw: str | None = None) -> bool:
    value = (os.environ.get(SCREEN_RECORD_ENV, "1") if raw is None else raw).strip().lower()
    return value in {"0", "false", "off"}


def should_keep_frame(last_t: float | None, now: float, fps: float) -> bool:
    if last_t is None:
        return True
    step = 1.0 / max(0.2, float(fps) or 1.0)
    return (now - last_t) >= step - 1e-6


def i420_size(width: int, height: int) -> int:
    return (int(width) * int(height) * 3) // 2


def even_wh(width: int, height: int) -> tuple[int, int]:
    return max(2, int(width) - int(width) % 2), max(2, int(height) - int(height) % 2)


def infer_i420_wh(nbytes: int, hint_w: int, hint_h: int) -> tuple[int, int] | None:
    hint_w, hint_h = even_wh(hint_w or 0, hint_h or 0)
    if nbytes == i420_size(hint_w, hint_h):
        return hint_w, hint_h
    if hint_w >= 2 and hint_h >= 2 and nbytes == i420_size(hint_w * 2, hint_h * 2):
        return hint_w * 2, hint_h * 2
    if nbytes * 2 % 3:
        return None
    pixels = nbytes * 2 // 3
    common = (
        (1920, 1080),
        (1600, 900),
        (1366, 768),
        (1280, 800),
        (1280, 720),
        (1024, 768),
        (960, 540),
        (854, 480),
        (640, 360),
    )
    for width, height in common:
        if i420_size(width, height) == nbytes:
            return width, height
    if hint_h >= 2 and pixels % hint_h == 0:
        width = pixels // hint_h
        if width >= 2 and width % 2 == 0:
            return width, hint_h
    return None


def pack_i420_stride(data: bytes, width: int, height: int) -> bytes | None:
    width, height = even_wh(width, height)
    if height < 2 or len(data) * 2 % (3 * height):
        return None
    stride = (len(data) * 2) // (3 * height)
    if stride < width or stride % 2:
        return None
    y_size = stride * height
    uv_stride = stride // 2
    uv_h = height // 2
    uv_size = uv_stride * uv_h
    if y_size + 2 * uv_size > len(data):
        return None
    u_off = y_size
    v_off = y_size + uv_size
    out = bytearray(i420_size(width, height))
    dest = 0
    for row in range(height):
        start = row * stride
        out[dest : dest + width] = data[start : start + width]
        dest += width
    uv_w = width // 2
    for plane in (u_off, v_off):
        for row in range(uv_h):
            start = plane + row * uv_stride
            out[dest : dest + uv_w] = data[start : start + uv_w]
            dest += uv_w
    return bytes(out)


def packed_i420(data: bytes, width: int, height: int) -> tuple[bytes, int, int] | None:
    if not data or width < 2 or height < 2:
        return None
    width, height = even_wh(width, height)
    need = i420_size(width, height)
    if len(data) == need:
        return data, width, height
    inferred = infer_i420_wh(len(data), width, height)
    if inferred:
        return data[: i420_size(*inferred)], inferred[0], inferred[1]
    packed = pack_i420_stride(data, width, height)
    if packed:
        return packed, width, height
    return None


def scale_i420(src: bytes, src_w: int, src_h: int, dst_w: int, dst_h: int) -> bytes:
    src_w, src_h = even_wh(src_w, src_h)
    dst_w, dst_h = even_wh(dst_w, dst_h)
    if (src_w, src_h) == (dst_w, dst_h) and len(src) == i420_size(src_w, src_h):
        return src

    def plane(buf: bytes, width: int, height: int, qw: int, qh: int) -> bytes:
        out = bytearray(qw * qh)
        for row in range(qh):
            sy = min(height - 1, row * height // qh)
            src_row = sy * width
            dest_row = row * qw
            for col in range(qw):
                sx = min(width - 1, col * width // qw)
                out[dest_row + col] = buf[src_row + sx]
        return bytes(out)

    y = src[: src_w * src_h]
    uv_w, uv_h = src_w // 2, src_h // 2
    mid = src_w * src_h
    u = src[mid : mid + uv_w * uv_h]
    v = src[mid + uv_w * uv_h : mid + 2 * uv_w * uv_h]
    return (
        plane(y, src_w, src_h, dst_w, dst_h)
        + plane(u, uv_w, uv_h, dst_w // 2, dst_h // 2)
        + plane(v, uv_w, uv_h, dst_w // 2, dst_h // 2)
    )


def ffmpeg_cmd(path: Path, width: int, height: int, fps: float, bitrate: int) -> list[str]:
    exe = shutil.which("ffmpeg") or "ffmpeg"
    rate = max(500_000, int(bitrate))
    return [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-s",
        f"{int(width)}x{int(height)}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-tune",
        "stillimage",
        "-b:v",
        str(rate),
        "-maxrate",
        str(rate),
        "-bufsize",
        str(rate * 2),
        "-x264-params",
        "keyint=1:min-keyint=1:scenecut=0",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]


KIND_AUDIO = 1
KIND_VIDEO = 2
SOURCE_CAMERA = 1
SOURCE_SCREENSHARE = 3


def _enum_int(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    raw = getattr(value, "value", None)
    if isinstance(raw, int):
        return raw
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_screen_video(track, pub) -> bool:
    kind = getattr(track, "kind", None)
    if kind is None:
        kind = getattr(pub, "kind", None)
    source = getattr(pub, "source", None)
    if source is None:
        source = getattr(track, "source", None)
    kind_i = _enum_int(kind)
    source_i = _enum_int(source)
    kind_s = str(kind or "").upper()
    source_s = str(source or "").upper()
    is_audio = kind_i == KIND_AUDIO or "AUDIO" in kind_s
    is_video = kind_i == KIND_VIDEO or "VIDEO" in kind_s
    if is_audio or not is_video:
        return False
    if source_i == SOURCE_CAMERA:
        return False
    if "CAMERA" in source_s and "SCREEN" not in source_s:
        return False
    return source_i == SOURCE_SCREENSHARE or "SCREEN" in source_s


def _env(name: str) -> str:
    path = ROOT / ".env"
    if not path.exists():
        return os.environ.get(name, "")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return os.environ.get(name, "")


def _recordings():
    global _REC_MOD
    if _REC_MOD is None:
        import importlib.util

        path = ROOT / "app" / "app" / "services" / "meeting_recordings.py"
        spec = importlib.util.spec_from_file_location("skkkk_meeting_recordings_screen", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _REC_MOD = mod
    return _REC_MOD


def _data_dir() -> Path:
    return Path(_env("DATA_DIR") or str(ROOT / "data"))


def _heartbeat_path() -> Path:
    return _data_dir() / "locks" / "screen_recorder.heartbeat"


def _touch_heartbeat() -> None:
    try:
        path = _heartbeat_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass


def _pub_sid(pub, track) -> str:
    return str(
        getattr(pub, "sid", None)
        or getattr(track, "sid", None)
        or getattr(pub, "track_sid", None)
        or ""
    )


def _video_stream(rtc, track):
    i420 = getattr(getattr(rtc, "VideoBufferType", None), "I420", None)
    try:
        if i420 is not None:
            return rtc.VideoStream(track, format=i420)
    except TypeError:
        pass
    return rtc.VideoStream(track)


def _frame_i420(frame):
    from livekit import rtc

    wanted = getattr(rtc, "VideoBufferType", None)
    i420 = getattr(wanted, "I420", None) if wanted is not None else None
    src = frame
    if i420 is not None and hasattr(frame, "convert"):
        try:
            src = frame.convert(i420)
        except Exception:
            src = frame
    data = bytes(getattr(src, "data", b"") or b"")
    width = int(getattr(src, "width", 0) or 0)
    height = int(getattr(src, "height", 0) or 0)
    packed = packed_i420(data, width, height)
    if packed:
        return packed
    return b"", 0, 0


async def _pump_video(track, writer) -> None:
    from livekit import rtc

    stream = _video_stream(rtc, track)
    try:
        async for event in stream:
            frame = getattr(event, "frame", event)
            writer.consume(frame)
    finally:
        await stream.aclose()
        writer.close()


class ScreenWriter:
    def __init__(
        self,
        path: Path,
        fps: float,
        canvas_w: int,
        canvas_h: int,
        bitrate: int,
        on_first,
    ) -> None:
        self.path = path
        self.fps = fps
        self.bitrate = bitrate
        self.on_first = on_first
        self.proc: subprocess.Popen | None = None
        self._err: object | None = None
        self.frames = 0
        self.width, self.height = even_wh(canvas_w, canvas_h)
        self.last_t: float | None = None
        self.started_epoch: float | None = None

    def consume(self, frame) -> None:
        now = time.time()
        if not should_keep_frame(self.last_t, now, self.fps):
            return
        data, width, height = _frame_i420(frame)
        if not data or width < 2 or height < 2:
            return
        if (width, height) != (self.width, self.height):
            data = scale_i420(data, width, height, self.width, self.height)
        if self.proc is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            err_path = self.path.with_suffix(".ffmpeg.log")
            self._err = err_path.open("ab")
            self.proc = subprocess.Popen(
                ffmpeg_cmd(self.path, self.width, self.height, self.fps, self.bitrate),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._err,
            )
            self.started_epoch = now
            if self.on_first:
                self.on_first(now, self.width, self.height)
        need = i420_size(self.width, self.height)
        if len(data) != need:
            print(
                "screen_recorder: skip frame",
                len(data),
                "want",
                need,
                f"{self.width}x{self.height}",
                flush=True,
            )
            return
        if self.proc.stdin is None:
            return
        try:
            self.proc.stdin.write(data)
        except BrokenPipeError:
            return
        self.last_t = now
        self.frames += 1

    def close(self) -> None:
        proc = self.proc
        err = self._err
        self.proc = None
        self._err = None
        if proc is None:
            if err is not None:
                try:
                    err.close()
                except Exception:
                    pass
            return
        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=FFMPEG_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
        if err is not None:
            try:
                err.close()
            except Exception:
                pass


def replace_async_task(tasks: dict, key: str, factory):
    old = tasks.get(key)
    if old is not None and not old.done():
        old.cancel()
    tasks[key] = factory()
    return tasks[key]


def _bind_recorders(room_name: str, rec, data_dir: Path):
    if rec is None:
        def _noop(*_args, **_kwargs):
            return None

        async def _noop_close() -> None:
            return None

        return _noop, _noop, _noop_close, None

    writers: dict[str, ScreenWriter] = {}
    eff_sids: dict[str, str] = {}  # исходный sid -> sid фрагмента (s2-..., s3-...), чтобы новый показ экрана не затирал прошлый файл
    tasks: dict[str, asyncio.Task] = {}
    manifest = rec.empty_manifest(room_name)
    events = rec.MeetingEventLog(rec.room_dir(data_dir, room_name) / rec.EVENTS_NAME)
    fps = rec.screen_fps()
    canvas_w, canvas_h = rec.screen_canvas()
    bitrate = rec.screen_bitrate()

    def persist() -> None:
        dest = rec.room_dir(data_dir, room_name) / rec.MANIFEST_NAME
        rec.write_manifest_merged(dest, manifest)

    def start_record(track, pub, participant) -> None:
        identity = participant.identity
        if not rec.should_record_identity(identity):
            return
        if not rec.screen_recording_enabled():
            return
        if not is_screen_video(track, pub):
            kind = getattr(track, "kind", None)
            if _enum_int(kind) == KIND_VIDEO or "VIDEO" in str(kind or "").upper():
                print("screen_recorder: skip video", identity, kind, getattr(pub, "source", None), flush=True)
            return
        sid = _pub_sid(pub, track)
        if not sid:
            return
        key = identity + "|" + sid
        if key in writers:
            replace_async_task(tasks, key, lambda: asyncio.create_task(_pump_video(track, writers[key])))
            return
        # показ экрана включали и выключали: тот же track_sid, но файл прошлого фрагмента трогать нельзя
        eff_sid = sid
        for n in range(2, 200):
            existing = rec.room_dir(data_dir, room_name) / rec.screen_video_relpath(identity, eff_sid)
            if not existing.is_file() or existing.stat().st_size == 0:
                break
            eff_sid = f"s{n}-{sid}"
        eff_sids[key] = eff_sid
        rel = rec.screen_video_relpath(identity, eff_sid)
        path = rec.room_dir(data_dir, room_name) / rel

        def on_first(started: float, width: int, height: int, who=identity, track_sid=eff_sid, rel_path=rel) -> None:
            rec.upsert_screen_video(
                manifest,
                identity=who,
                rel_path=rel_path,
                track_sid=track_sid,
                started_epoch=started,
                fps=fps,
                width=width,
                height=height,
            )
            persist()
            events.append(
                rec.EVENT_TRACK,
                identity=who,
                event="start",
                source=rec.SOURCE_SCREEN_VIDEO,
                track_sid=track_sid,
            )
            print("screen_recorder: record", room_name, who, track_sid, flush=True)

        writer = ScreenWriter(path, fps, canvas_w, canvas_h, bitrate, on_first)
        writers[key] = writer
        replace_async_task(tasks, key, lambda: asyncio.create_task(_pump_video(track, writer)))

    def stop_record(pub, participant) -> None:
        sid = _pub_sid(pub, getattr(pub, "track", None))
        key = participant.identity + "|" + sid
        sid = eff_sids.pop(key, sid)
        task = tasks.pop(key, None)
        if task:
            task.cancel()
        writer = writers.pop(key, None)
        if writer is None:
            return
        writer.close()
        if writer.started_epoch is None:
            return
        rec.set_screen_video_frames(
            manifest,
            participant.identity,
            sid,
            writer.frames,
            fps,
            ended_epoch=time.time(),
            width=writer.width,
            height=writer.height,
        )
        persist()
        events.append(
            rec.EVENT_TRACK,
            identity=participant.identity,
            event="end",
            source=rec.SOURCE_SCREEN_VIDEO,
            track_sid=sid,
            frames=writer.frames,
        )

    async def close_all() -> None:
        pending = list(tasks.values())
        for task in pending:
            task.cancel()
        tasks.clear()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for key, writer in list(writers.items()):
            writer.close()
            identity, sid = key.split("|", 1)
            sid = eff_sids.get(key, sid)
            if writer.started_epoch is None:
                continue
            rec.set_screen_video_frames(
                manifest,
                identity,
                sid,
                writer.frames,
                fps,
                ended_epoch=time.time(),
                width=writer.width,
                height=writer.height,
            )
            events.append(
                rec.EVENT_TRACK,
                identity=identity,
                event="end",
                source=rec.SOURCE_SCREEN_VIDEO,
                track_sid=sid,
                frames=writer.frames,
            )
        writers.clear()
        if any(row.get("source") == rec.SOURCE_SCREEN_VIDEO for row in manifest.get("tracks") or []):
            persist()

    return start_record, stop_record, close_all, events


async def _join(url: str, token: str, room_name: str) -> None:
    from livekit import rtc

    room = rtc.Room()
    rec_mod = _recordings()
    rec = rec_mod if rec_mod.screen_recording_enabled() else None
    start_record, stop_record, close_records, _events = _bind_recorders(room_name, rec, _data_dir())

    def _is_human(identity: str) -> bool:
        return rec_mod.is_human_identity(identity)

    @room.on("track_subscribed")
    def _on_track(track, _pub, participant) -> None:
        if not _is_human(getattr(participant, "identity", "")):
            return
        start_record(track, _pub, participant)

    @room.on("track_unsubscribed")
    def _on_unsub(track, _pub, participant) -> None:
        stop_record(_pub, participant)

    @room.on("track_published")
    def _on_pub(pub, participant) -> None:
        if not _is_human(getattr(participant, "identity", "")):
            return
        setter = getattr(pub, "set_subscribed", None)
        if setter:
            try:
                setter(True)
            except Exception as exc:
                print("screen_recorder: subscribe", exc, flush=True)
        track = getattr(pub, "track", None)
        if track is not None:
            start_record(track, pub, participant)

    @room.on("participant_connected")
    def _on_person(participant) -> None:
        _scan_person(participant)

    def _scan_person(participant) -> None:
        if not _is_human(getattr(participant, "identity", "")):
            return
        pubs = getattr(participant, "track_publications", None) or {}
        values = pubs.values() if hasattr(pubs, "values") else pubs
        for pub in list(values):
            setter = getattr(pub, "set_subscribed", None)
            if setter:
                try:
                    setter(True)
                except Exception:
                    pass
            track = getattr(pub, "track", None)
            if track is not None:
                start_record(track, pub, participant)

    await room.connect(url, token)
    print("screen_recorder: joined", room.name, flush=True)
    remotes = getattr(room, "remote_participants", None) or {}
    people = remotes.values() if hasattr(remotes, "values") else remotes
    for person in list(people):
        _scan_person(person)
    try:
        while True:
            if room.connection_state != rtc.ConnectionState.CONN_CONNECTED:
                return
            await asyncio.sleep(POLL_SEC)
    finally:
        await close_records()
        await room.disconnect()


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
                "sub": RECORDER_ID,
                "nbf": now,
                "exp": now + 21600,
                "name": RECORDER_NAME,
                "video": {
                    "roomJoin": True,
                    "room": room_name,
                    "canSubscribe": True,
                    "canPublish": False,
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


async def _watch(name: str, token: str, active: set[str], backoff=None) -> None:
    try:
        await _join(INTERNAL_URL, token, name)
    except Exception as exc:
        print("screen_recorder:", name, exc, file=sys.stderr)
    finally:
        active.discard(name)
        if backoff is not None:
            backoff.finished(name)


async def _loop() -> None:
    key = _env("LIVEKIT_API_KEY")
    secret = _env("LIVEKIT_API_SECRET")
    if not key or not secret:
        print("screen_recorder: no LiveKit keys", file=sys.stderr)
        return
    print("screen_recorder: ready fps", _recordings().screen_fps(), flush=True)
    active: set[str] = set()
    backoff = _recordings().JoinBackoff()
    while True:
        _touch_heartbeat()
        list_jwt, _ = _tokens(key, secret)
        try:
            rooms = _list_rooms(list_jwt)
        except Exception as exc:
            print("screen_recorder: list", exc, file=sys.stderr)
            rooms = []
        backoff.forget_missing(rooms)
        for name in rooms:
            if name in active or not backoff.allowed(name):
                continue
            _, join_jwt = _tokens(key, secret, name)
            active.add(name)
            backoff.started(name)
            asyncio.create_task(_watch(name, join_jwt, active, backoff))
        await asyncio.sleep(POLL_SEC)


def main() -> int:
    if main_disabled():
        print("screen_recorder: off", flush=True)
        return 0
    # одна копия на машину: вторая с тем же именем участника выбивала бы первую из комнаты
    instance = _recordings().acquire_single_instance(_data_dir() / "locks" / "screen_recorder.instance")
    if instance is None:
        print("screen_recorder: уже запущен, эта копия выходит", flush=True)
        return 0
    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        return 0
    finally:
        instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
