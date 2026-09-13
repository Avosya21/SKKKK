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
    raw = str(getattr(pub, "source", "") or "").upper()
    if "SCREEN" in raw:
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


class PcmWavWriter:
    """WAV, растянутый по стенным часам: пока микрофон молчит, пишется тишина.

    Иначе кадры ложатся вплотную, дорожка выходит короче встречи, и всё после
    паузы съезжает назад по времени.
    """

    def __init__(
        self,
        path: Path,
        sample_rate: int = SAMPLE_RATE,
        clock: Callable[[], float] = time.monotonic,
        on_gap: Callable[[float], None] | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.sample_rate = sample_rate
        self.frames = 0
        self._clock = clock
        self._on_gap = on_gap
        self._opened = clock()
        self._wave = wave.open(str(path), "wb")
        self._wave.setnchannels(CHANNELS)
        self._wave.setsampwidth(SAMPLE_WIDTH)
        self._wave.setframerate(sample_rate)

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

    def _write_frames(self, pcm16: bytes) -> None:
        self._wave.writeframes(pcm16)
        self.frames += len(pcm16) // SAMPLE_WIDTH

    def close(self) -> None:
        if self._wave is None:
            return
        self._wave.close()
        self._wave = None
