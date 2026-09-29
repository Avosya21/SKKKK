"""Raw per-speaker audio for one LiveKit room. Not DFN. Not ASR."""

from __future__ import annotations

import json
import os
import shutil
import time
import wave
from collections.abc import Callable
from pathlib import Path

CLEANER_ID = "skkkk-dfn"
HIDDEN_PREFIX = "skkkk-"
RECORD_DIRNAME = "meetings"
TRACKS_DIRNAME = "tracks"
MANIFEST_NAME = "manifest.json"
EVENTS_NAME = "events.jsonl"
RECORD_ENV = "MEETING_RECORD"
SAMPLE_RATE = 48000
CHANNELS = 1
SAMPLE_WIDTH = 2
PCM_MAX = 32768.0
SOURCE_MICROPHONE = "microphone"
SOURCE_SCREEN = "screen"
SOURCE_SCREEN_VIDEO = "screen_video"
RECORDER_ID = "skkkk-rec"
SCREEN_RECORD_ENV = "MEETING_SCREEN_RECORD"
SCREEN_FPS_ENV = "MEETING_SCREEN_FPS"
SCREEN_FPS_DEFAULT = 1.0
SCREEN_MAX_W_ENV = "MEETING_SCREEN_MAX_W"
SCREEN_MAX_W_DEFAULT = 1920
SCREEN_HEIGHT_ENV = "MEETING_SCREEN_HEIGHT"
SCREEN_HEIGHT_DEFAULT = 1080
SCREEN_BITRATE_ENV = "MEETING_SCREEN_BITRATE"
SCREEN_BITRATE_DEFAULT = 5_000_000
SID_SAFE_LEN = 16
OFF_VALUES = {"0", "false", "off"}
GAP_PAD_SEC = 0.1
SPEECH_WINDOW_SEC = 1.0
SPEECH_MIN_SEC_ENV = "MEETING_SPEECH_MIN_SEC"
SPEECH_RMS_MIN_ENV = "MEETING_SPEECH_RMS_MIN"
SPEECH_MIN_SEC_DEFAULT = 2.0
SPEECH_RMS_MIN_DEFAULT = 0.01
EVENT_SPEAKERS = "speakers"
EVENT_MUTE = "mute"
EVENT_TRACK = "track"
EVENT_PARTICIPANT = "participant"
EVENT_QUALITY = "quality"
EVENT_GAP = "gap"
MEMBER_PREFIX = "member:"


def recording_enabled(raw: str | None = None) -> bool:
    value = (os.environ.get(RECORD_ENV, "1") if raw is None else raw).strip().lower()
    if not value:
        return True
    return value not in OFF_VALUES


def screen_recording_enabled(record_raw: str | None = None, screen_raw: str | None = None) -> bool:
    if not recording_enabled(record_raw):
        return False
    value = (
        os.environ.get(SCREEN_RECORD_ENV, "1") if screen_raw is None else screen_raw
    ).strip().lower()
    if not value:
        return True
    return value not in OFF_VALUES


def screen_fps(raw: str | None = None) -> float:
    text = (os.environ.get(SCREEN_FPS_ENV, "") if raw is None else raw).strip()
    if not text:
        return SCREEN_FPS_DEFAULT
    try:
        value = float(text)
    except ValueError:
        return SCREEN_FPS_DEFAULT
    return max(0.2, min(5.0, value))


def screen_max_width(raw: str | None = None) -> int:
    text = (os.environ.get(SCREEN_MAX_W_ENV, "") if raw is None else raw).strip()
    if not text:
        return SCREEN_MAX_W_DEFAULT
    try:
        value = int(float(text))
    except ValueError:
        return SCREEN_MAX_W_DEFAULT
    return max(640, min(1920, value - value % 2))


def screen_height(raw: str | None = None) -> int:
    text = (os.environ.get(SCREEN_HEIGHT_ENV, "") if raw is None else raw).strip()
    if not text:
        return SCREEN_HEIGHT_DEFAULT
    try:
        value = int(float(text))
    except ValueError:
        return SCREEN_HEIGHT_DEFAULT
    return max(360, min(1080, value - value % 2))


def screen_bitrate(raw: str | None = None) -> int:
    text = (os.environ.get(SCREEN_BITRATE_ENV, "") if raw is None else raw).strip().lower()
    if not text:
        return SCREEN_BITRATE_DEFAULT
    for suffix in ("mbps", "mbit", "mb/s", "m"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            try:
                return max(500_000, min(20_000_000, int(float(text) * 1_000_000)))
            except ValueError:
                return SCREEN_BITRATE_DEFAULT
    try:
        value = int(float(text))
    except ValueError:
        return SCREEN_BITRATE_DEFAULT
    if value < 1000:
        value *= 1_000_000
    return max(500_000, min(20_000_000, value))


def screen_canvas() -> tuple[int, int]:
    return screen_max_width(), screen_height()


def safe_sid(track_sid: str) -> str:
    cleaned = "".join(ch for ch in str(track_sid or "") if ch.isalnum() or ch in "-_")
    return (cleaned or "sid")[:SID_SAFE_LEN]


def screen_video_relpath(identity: str, track_sid: str) -> str:
    return f"{TRACKS_DIRNAME}/{safe_track_stem(identity, SOURCE_SCREEN_VIDEO)}-{safe_sid(track_sid)}.mp4"


def is_human_identity(identity: str) -> bool:
    return bool(identity) and not identity.startswith(HIDDEN_PREFIX)


def should_record_identity(identity: str) -> bool:
    return is_human_identity(identity)


def safe_track_stem(identity: str, source: str = SOURCE_MICROPHONE) -> str:
    stem = identity.replace(":", "-")
    if source != SOURCE_MICROPHONE:
        return f"{stem}-{source}"
    return stem


def recordings_root(data_dir: Path) -> Path:
    return data_dir / RECORD_DIRNAME


def room_dir(data_dir: Path, room_name: str) -> Path:
    if not room_name.startswith(HIDDEN_PREFIX):
        raise ValueError("room_name")
    if any(part in room_name for part in ("/", "\\", "..")):
        raise ValueError("room_name")
    return recordings_root(data_dir) / room_name


def track_path(
    data_dir: Path,
    room_name: str,
    identity: str,
    source: str = SOURCE_MICROPHONE,
) -> Path:
    name = safe_track_stem(identity, source) + ".wav"
    return room_dir(data_dir, room_name) / TRACKS_DIRNAME / name


def source_from_publication(pub) -> str:
    raw = getattr(pub, "source", "")
    try:
        code = int(raw)
    except (TypeError, ValueError):
        code = None
    if code in {3, 4}:
        return SOURCE_SCREEN
    text = str(raw or "").upper()
    if "SCREEN" in text:
        return SOURCE_SCREEN
    return SOURCE_MICROPHONE


def empty_manifest(room_name: str) -> dict:
    return {"room_name": room_name, "tracks": []}


def upsert_track(
    manifest: dict,
    *,
    identity: str,
    rel_path: str,
    source: str,
    sample_rate: int,
    started_epoch: float,
) -> dict:
    """``started_epoch`` — время первого сэмпла. У известной дорожки не трогаем."""
    tracks = manifest.setdefault("tracks", [])
    for row in tracks:
        if row.get("identity") == identity and row.get("source") == source:
            row["path"] = rel_path
            row["sample_rate"] = sample_rate
            return manifest
    tracks.append(
        {
            "identity": identity,
            "path": rel_path,
            "source": source,
            "sample_rate": sample_rate,
            "started_epoch": round(started_epoch, 3),
        }
    )
    return manifest


def upsert_screen_video(
    manifest: dict,
    *,
    identity: str,
    rel_path: str,
    track_sid: str,
    started_epoch: float,
    fps: float = SCREEN_FPS_DEFAULT,
    width: int = 0,
    height: int = 0,
) -> dict:
    """Ключ видео — (identity, screen_video, track_sid). started_epoch не перетираем."""
    tracks = manifest.setdefault("tracks", [])
    for row in tracks:
        if (
            row.get("identity") == identity
            and row.get("source") == SOURCE_SCREEN_VIDEO
            and row.get("track_sid") == track_sid
        ):
            row["path"] = rel_path
            if fps:
                row["fps"] = fps
            if width:
                row["width"] = width
            if height:
                row["height"] = height
            return manifest
    tracks.append(
        {
            "identity": identity,
            "source": SOURCE_SCREEN_VIDEO,
            "track_sid": track_sid,
            "path": rel_path,
            "started_epoch": round(started_epoch, 3),
            "fps": fps,
            "width": width,
            "height": height,
        }
    )
    return manifest


def set_screen_video_frames(
    manifest: dict,
    identity: str,
    track_sid: str,
    frames: int,
    fps: float,
    ended_epoch: float | None = None,
    width: int | None = None,
    height: int | None = None,
) -> None:
    rate = float(fps or SCREEN_FPS_DEFAULT) or SCREEN_FPS_DEFAULT
    for row in manifest.get("tracks") or []:
        if (
            row.get("identity") == identity
            and row.get("source") == SOURCE_SCREEN_VIDEO
            and row.get("track_sid") == track_sid
        ):
            row["frames"] = frames
            row["fps"] = rate
            row["duration_sec"] = round(frames / rate, 3)
            if ended_epoch is not None:
                row["ended_epoch"] = round(ended_epoch, 3)
            if width:
                row["width"] = width
            if height:
                row["height"] = height
            return


def set_track_frames(
    manifest: dict,
    identity: str,
    source: str,
    frames: int,
    sample_rate: int,
    ended_epoch: float | None = None,
) -> None:
    for row in manifest.get("tracks") or []:
        if row.get("identity") == identity and row.get("source") == source:
            row["frames"] = frames
            row["duration_sec"] = round(frames / sample_rate, 3)
            if ended_epoch is not None:
                row["ended_epoch"] = round(ended_epoch, 3)
            return


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def speech_min_sec() -> float:
    return _env_float(SPEECH_MIN_SEC_ENV, SPEECH_MIN_SEC_DEFAULT)


def speech_rms_min() -> float:
    return _env_float(SPEECH_RMS_MIN_ENV, SPEECH_RMS_MIN_DEFAULT)


def window_rms(path: Path, start_sec: float, end_sec: float) -> float:
    """RMS участка WAV. Нет файла / битый заголовок → 0, не исключение."""
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate() or 1
            if handle.getsampwidth() != SAMPLE_WIDTH:
                return 0.0
            start = max(0, int(float(start_sec) * rate))
            stop = max(start + 1, int(float(end_sec) * rate))
            handle.setpos(min(start, handle.getnframes()))
            frames = handle.readframes(max(1, stop - start))
    except (OSError, wave.Error, OverflowError):
        return 0.0
    if not frames:
        return 0.0
    import array

    samples = array.array("h")
    samples.frombytes(frames)
    if not samples:
        return 0.0
    return (sum(item * item for item in samples) / len(samples)) ** 0.5 / PCM_MAX


def has_speech(path: Path, *, min_sec: float | None = None, rms_min: float | None = None) -> bool:
    """Речь = несколько секундных окон выше RMS. Тишину на ASR не гоним."""
    need = speech_min_sec() if min_sec is None else min_sec
    floor = speech_rms_min() if rms_min is None else rms_min
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate() or 1
            width = handle.getsampwidth()
            frames = handle.readframes(handle.getnframes())
    except (OSError, wave.Error):
        return False
    if width != SAMPLE_WIDTH or not frames:
        return False
    import array

    samples = array.array("h")
    samples.frombytes(frames)
    window = max(1, int(rate * SPEECH_WINDOW_SEC))
    spoken = 0.0
    for start in range(0, len(samples), window):
        chunk = samples[start : start + window]
        if not chunk:
            continue
        rms = (sum(item * item for item in chunk) / len(chunk)) ** 0.5 / PCM_MAX
        if rms >= floor:
            spoken += 1.0
            if spoken >= need:
                return True
    return False


def speaker_group_key(identity: str) -> str:
    """member: стабильный user_id. guest: только session identity, не display_name."""
    return str(identity or "")


def speakers_changed(prev: list[str] | None, new: list[str] | None) -> bool:
    return tuple(prev or []) != tuple(new or [])


class MeetingEventLog:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, kind: str, **fields) -> dict:
        row = {"t": round(float(self._clock()), 3), "kind": kind}
        row.update(fields)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row


def read_events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def events_rel(room_name: str) -> str:
    return f"{RECORD_DIRNAME}/{room_name}/{EVENTS_NAME}"


def write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _track_merge_key(row: dict) -> tuple:
    if row.get("source") == SOURCE_SCREEN_VIDEO:
        return ("v", str(row.get("identity") or ""), str(row.get("track_sid") or ""))
    return ("a", str(row.get("identity") or ""), str(row.get("source") or SOURCE_MICROPHONE))


def merge_track_rows(base: list, incoming: list) -> list:
    by: dict[tuple, dict] = {}
    for row in list(base or []) + list(incoming or []):
        if isinstance(row, dict):
            by[_track_merge_key(row)] = row
    return list(by.values())


def write_manifest_merged(path: Path, manifest: dict) -> dict:
    """Не затирает чужие дорожки: voice_cleaner и skkkk-rec пишут один файл."""
    dest = path.parent / (path.name + ".lock")

    def _write() -> dict:
        existing = read_manifest(path) or {}
        merged = dict(manifest)
        if existing.get("room_name") and not merged.get("room_name"):
            merged["room_name"] = existing["room_name"]
        merged["tracks"] = merge_track_rows(existing.get("tracks") or [], manifest.get("tracks") or [])
        write_manifest(path, merged)
        manifest["tracks"] = merged["tracks"]
        return merged

    dest.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(40):
        try:
            handle = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(handle)
            try:
                return _write()
            finally:
                dest.unlink(missing_ok=True)
        except FileExistsError:
            time.sleep(0.05)
    return _write()


def acquire_single_instance(lock_path: Path):
    """Не даёт запустить вторую копию рекордера: эксклюзивная блокировка файла на всё время жизни процесса.

    Две копии с одним именем участника в LiveKit выбивают друг друга из комнаты («duplicate participant»), дорожки обрываются.
    Блокировка снимается ОС при любой смерти процесса, «протухших» замков не бывает. Возвращает открытый файл (держать до конца
    процесса) или None, если экземпляр уже работает."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()).encode("ascii"))
        fh.flush()
    except OSError:
        pass
    return fh


class JoinBackoff:
    """Пауза для комнат, куда не удаётся подключиться (завершённые встречи, которые LiveKit ещё держит).

    Подключение короче ``min_ok`` секунд считается неудачей: следующая попытка через base*2^(n-1) секунд, не дольше cap.
    Долгое подключение сбрасывает счётчик. Без этого рекордер тысячами заходил в комнаты давно закрытых встреч."""

    def __init__(self, base: float = 30.0, cap: float = 1800.0, min_ok: float = 20.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.base, self.cap, self.min_ok, self._clock = base, cap, min_ok, clock
        self._started: dict[str, float] = {}
        self._streak: dict[str, int] = {}
        self._next: dict[str, float] = {}

    def allowed(self, room: str) -> bool:
        return self._clock() >= self._next.get(room, 0.0)

    def started(self, room: str) -> None:
        self._started[room] = self._clock()

    def finished(self, room: str) -> None:
        lasted = self._clock() - self._started.pop(room, self._clock())
        if lasted >= self.min_ok:
            self._streak.pop(room, None)
            self._next.pop(room, None)
            return
        n = self._streak.get(room, 0) + 1
        self._streak[room] = n
        self._next[room] = self._clock() + min(self.cap, self.base * (2 ** (n - 1)))

    def forget_missing(self, listed: list[str]) -> None:
        keep = set(listed)
        for store in (self._streak, self._next, self._started):
            for room in [r for r in store if r not in keep]:
                store.pop(room, None)


MEMBER_CHECK_SEC = 10.0


def room_member_state(http_url: str, admin_jwt: str, room_name: str, identity: str, timeout: float = 5.0) -> bool | None:
    """Числится ли identity в комнате по данным сервера LiveKit (ListParticipants). False — нет его или нет комнаты;
    None — сервер не ответил (это не повод выходить: пусть решает связь самого SDK)."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        http_url.rstrip("/") + "/twirp/livekit.RoomService/ListParticipants",
        data=json.dumps({"room": room_name}).encode("utf-8"),
        headers={"Authorization": "Bearer " + admin_jwt, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except (OSError, ValueError):
        return None
    return any((row.get("identity") or "") == identity for row in payload.get("participants") or [])


class MemberWatch:
    """Бот рекордера/шумодава выпал из комнаты, а SDK всё ещё считает связь живой (замер 2026-09-30: после разрыва
    connection_state остался CONNECTED, бот не вернулся, запись дорожек встала на 80 с). Раз в ``every`` секунд
    спрашиваем сервер; ``misses`` ответов «тебя нет» подряд — выходим, цикл бота зайдёт заново."""

    def __init__(self, every: float = MEMBER_CHECK_SEC, misses: int = 2, clock: Callable[[], float] = time.monotonic) -> None:
        self.every, self.misses, self._clock = every, misses, clock
        self._last = clock()
        self._missed = 0

    def due(self) -> bool:
        if self._clock() - self._last < self.every:
            return False
        self._last = self._clock()
        return True

    def lost(self, state: bool | None) -> bool:
        if state is False:
            self._missed += 1
        elif state is True:
            self._missed = 0
        return self._missed >= self.misses


def live_room_names(data_dir: Path) -> set[str] | None:
    """Комнаты идущих встреч (``meetings.status = 'live'``) из базы портала, только чтение. Рекордеры заходят только в них:
    комнату завершённой или удалённой встречи может заново создать клиент со старым токеном LiveKit (переподключение
    в момент завершения), и боты, зайдя туда, держали её открытой и писали запись удалённой встречи на диск.
    None — базу прочитать не удалось: пишем все комнаты, как раньше (лучше лишняя запись, чем потерянный созвон)."""
    import sqlite3

    db = data_dir / "app.db"
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            return {row[0] for row in con.execute("SELECT room_name FROM meetings WHERE status = 'live'")}
        finally:
            con.close()
    except sqlite3.Error:
        return None


def existing_track_started(data_dir: Path, room_name: str, identity: str, source: str) -> float | None:
    """Время первого сэмпла уже записанной дорожки (для продолжения записи после перезапуска рекордера)."""
    manifest = read_manifest(room_dir(data_dir, room_name) / MANIFEST_NAME) or {}
    for row in manifest.get("tracks") or []:
        if row.get("identity") == identity and (row.get("source") or SOURCE_MICROPHONE) == source and row.get("started_epoch"):
            return float(row["started_epoch"])
    return None


def read_manifest(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def tracks_linked_to_room(manifest: dict, room_name: str) -> list[dict]:
    if manifest.get("room_name") != room_name:
        return []
    return list(manifest.get("tracks") or [])


def tracks_for_meeting(data_dir: Path, room_name: str) -> list[dict]:
    data = read_manifest(room_dir(data_dir, room_name) / MANIFEST_NAME)
    if not data:
        return []
    return tracks_linked_to_room(data, room_name)


def remove_room_recordings(data_dir: Path, room_name: str) -> None:
    target = room_dir(data_dir, room_name)
    root = recordings_root(data_dir).resolve()
    try:
        resolved = target.resolve()
    except OSError:
        return
    if resolved.parent != root or not resolved.is_dir():
        return
    shutil.rmtree(resolved)


_WAV_HEADER = 44
HEADER_FLUSH_SEC = 2.0  # как часто дописываем в заголовок реальный размер: после аварийного завершения файл читается


def _wav_header(sample_rate: int, data_bytes: int) -> bytes:
    import struct

    byte_rate = sample_rate * CHANNELS * SAMPLE_WIDTH
    return (
        b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, CHANNELS, sample_rate, byte_rate, CHANNELS * SAMPLE_WIDTH, SAMPLE_WIDTH * 8)
        + b"data" + struct.pack("<I", data_bytes)
    )


class PcmWavWriter:
    """WAV, растянутый по стенным часам: пока микрофон молчит, пишется тишина.

    Иначе кадры ложатся вплотную, дорожка выходит короче встречи, и всё после
    паузы съезжает назад по времени.

    Файл НИКОГДА не обнуляется: если процесс записи перезапустили (или участник перезашёл) и файл уже есть,
    запись продолжается в его конец, а пропущенное время заполняется тишиной (нужен ``resume_started_epoch`` —
    время первого сэмпла дорожки). Заголовок обновляется каждые ``HEADER_FLUSH_SEC``, поэтому при падении процесса
    теряются секунды, а не вся дорожка.
    """

    def __init__(
        self,
        path: Path,
        sample_rate: int = SAMPLE_RATE,
        clock: Callable[[], float] = time.monotonic,
        on_gap: Callable[[float], None] | None = None,
        resume_started_epoch: float | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.sample_rate = sample_rate
        self.frames = 0
        self._clock = clock
        self._on_gap = on_gap
        self._last_header = clock()
        self.resumed = False
        existing = path.stat().st_size if path.is_file() else 0
        if existing > _WAV_HEADER and self._header_matches(path):
            self._file = open(path, "r+b")
            self.frames = (existing - _WAV_HEADER) // SAMPLE_WIDTH
            self._file.seek(_WAV_HEADER + self.frames * SAMPLE_WIDTH)
            self._file.truncate()  # обрезать неполный сэмпл в хвосте, если он есть
            self.resumed = True
            if resume_started_epoch:  # время, пока запись не шла (перезапуск), заполняем тишиной
                lost = wall_clock() - (resume_started_epoch + self.frames / sample_rate)
                if lost >= GAP_PAD_SEC:
                    self._write_frames(b"\x00" * (int(lost * sample_rate) * SAMPLE_WIDTH))
                    if self._on_gap:
                        self._on_gap(lost)
        else:
            if existing:  # файл есть, но это не наш WAV: не затираем, откладываем в сторону
                path.rename(path.with_name(f"{path.stem}.unreadable-{int(wall_clock())}{path.suffix}"))
            self._file = open(path, "wb")
            self._file.write(_wav_header(sample_rate, 0))
        self._opened = clock() - self.frames / sample_rate
        self._patch_header()

    def _header_matches(self, path: Path) -> bool:
        try:
            with open(path, "rb") as fh:
                head = fh.read(_WAV_HEADER)
        except OSError:
            return False
        return len(head) == _WAV_HEADER and head[:4] == b"RIFF" and head[8:16] == b"WAVEfmt " and head[36:40] == b"data" and head[24:28] == self.sample_rate.to_bytes(4, "little")

    def _patch_header(self) -> None:
        if self._file is None:
            return
        pos = self._file.tell()
        self._file.seek(0)
        self._file.write(_wav_header(self.sample_rate, self.frames * SAMPLE_WIDTH))
        self._file.seek(pos)
        self._file.flush()
        self._last_header = self._clock()

    def _gap_frames(self) -> int:
        """Сколько сэмплов потеряно. Мелкий дребезг кадров не считаем."""
        missing = int((self._clock() - self._opened) * self.sample_rate) - self.frames
        return missing if missing >= int(GAP_PAD_SEC * self.sample_rate) else 0

    def write(self, pcm16: bytes) -> None:
        gap = self._gap_frames()
        if gap:
            self._write_frames(b"\x00" * (gap * SAMPLE_WIDTH))
            if self._on_gap:
                self._on_gap(gap / self.sample_rate)
        self._write_frames(pcm16)
        if self._clock() - self._last_header >= HEADER_FLUSH_SEC:
            self._patch_header()

    def _write_frames(self, pcm16: bytes) -> None:
        self._file.write(pcm16)
        self.frames += len(pcm16) // SAMPLE_WIDTH

    def close(self) -> None:
        if self._file is None:
            return
        self._patch_header()
        self._file.close()
        self._file = None
