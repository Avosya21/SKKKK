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
QUEUE_MS = 120  # потолок буфера выхода: он идёт в реальном времени, всё, что в нём лежит, — задержка
FRAME_MS = 10
CHUNK_MS = 80
POLL_SEC = 2.0
DENOISE_KEY = "denoise"
DENOISE_TOPIC = "skkkk-denoise"
ATTEN_ENV = "MEETING_VOICE_ATTEN_LIM"
POST_ENV = "MEETING_VOICE_POST_FILTER"
MAX_TRACKS_ENV = "MEETING_VOICE_MAX_TRACKS"
# Кусок 80 мс на CPU считается ~8 мс в одном потоке событий: 8 дорожек на все комнаты — ~80 % времени.
# Сверх предела человек идёт без очистки (у слушателей играет его сырой звук), а не с растущим отставанием.
MAX_TRACKS_DEFAULT = 8
# Прошлые кадры окна потокового DFN3: входная свёртка (3), df_convp (5) и порядок DF (5) смотрят назад.
LIVE_CONTEXT = 8
# Вход копится, если цикл событий застрял (прогрев модели, соседние дорожки), а выход AudioSource идёт строго
# в реальном времени — накопленное само не рассосётся и навсегда станет задержкой (замер: +1.9…2.7 с).
# Больше BACKLOG_FRAMES кадров по 10 мс в очереди — лишнее выбрасываем; в норме там до ~7 (8 дорожек ждут замок).
BACKLOG_FRAMES = 15
BACKLOG_KEEP = 2
STREAM_CAP_FRAMES = 50  # жёсткий потолок очереди входа: RingQueue сам выбрасывает старые кадры
STATS_EVERY_S = 60.0
PUBLISH_TIMEOUT_S = 10.0
ATTEN_DEFAULT = 20.0
INTERNAL_URL = os.environ.get("LIVEKIT_RTC_INTERNAL", "ws://127.0.0.1:7880")
HTTP_URL = os.environ.get("LIVEKIT_HTTP_INTERNAL", "http://127.0.0.1:7880")
_ATTEN = ATTEN_DEFAULT
_MAX_TRACKS = MAX_TRACKS_DEFAULT
_BUSY: dict = {}  # "комната|identity" -> задача очистки, на все комнаты процесса


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


def max_tracks(raw: str | None = None) -> int:
    try:
        value = int(str(raw if raw is not None else "").strip() or MAX_TRACKS_DEFAULT)
    except ValueError:
        return MAX_TRACKS_DEFAULT
    return max(1, value)


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
        common = sys.modules["torchaudio.backend.common"]
        existing = getattr(common, "AudioMetaData", None)
        if existing is not None:
            try:
                existing(sample_rate=0)
                return
            except TypeError:
                class CompatAudioMetaData(existing):
                    def __init__(self, sample_rate=0, num_frames=0, num_channels=0,
                                 bits_per_sample=0, encoding=""):
                        super().__init__(sample_rate, num_frames, num_channels,
                                         bits_per_sample, encoding)
                common.AudioMetaData = CompatAudioMetaData
                return
    try:
        from torchaudio.backend.common import AudioMetaData
        # torchaudio 2.9 keeps the class but made all metadata fields required;
        # DeepFilterNet and older callers rely on the legacy defaults.
        try:
            AudioMetaData(sample_rate=0)
        except TypeError:
            common = sys.modules["torchaudio.backend.common"]
            class CompatAudioMetaData(AudioMetaData):
                def __init__(self, sample_rate=0, num_frames=0, num_channels=0,
                             bits_per_sample=0, encoding=""):
                    super().__init__(sample_rate, num_frames, num_channels,
                                     bits_per_sample, encoding)
            common.AudioMetaData = CompatAudioMetaData
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
    # Compensate STFT delay so the chunk stays the same length. A short
    # enhance output is zero-padded by _fit and the live voice falls apart.
    out = enhance(model, state, audio, pad=True, atten_lim_db=atten)
    wave = out.detach().cpu().numpy().reshape(-1)
    wave = np.clip(wave, -1.0, 1.0)
    return _fit((wave * 32767.0).astype(np.int16), len(pcm))


_LIVE_GRUS = (("enc", "emb_gru"), ("erb_dec", "emb_gru"), ("df_dec", "df_gru"))
_LIVE_WINDOW: dict = {}


def _live_gru_forward(key: str):
    def forward(self, input, h=None):
        window = _LIVE_WINDOW.get("now")
        if window is None:
            return type(self).forward(self, input, h)
        import torch

        c, split = window["c"], window["split"]
        out = torch.zeros(input.shape[0], input.shape[1], window["dims"][key], dtype=input.dtype)
        if split > c:
            x = self.linear_in(input[:, c:split])
            y, h_new = self.gru(x, window["h"].get(key))
            y = self.linear_out(y)
            if self.gru_skip is not None:
                y = y + self.gru_skip(input[:, c:split])
            out[:, c:split] = y
            window["h"][key] = h_new
        return out, None

    return forward


def _install_live_grus(model) -> bool:
    """GRU DeepFilterNet3 продолжают с памяти дорожки: кадры контекста не шагают их повторно, хвост без заглядывания
    вперёд не портит память. Нет нужных слоёв (другая модель) — False, живём покусочно через _enhance."""
    if getattr(model, "_skkkk_live_dims", None):
        return True
    import types

    import torch

    dims = {}
    try:
        mods = []
        for owner, name in _LIVE_GRUS:
            mod = getattr(getattr(model, owner), name)
            for attr in ("linear_in", "gru", "linear_out", "gru_skip"):
                getattr(mod, attr)
            with torch.no_grad():
                dims[owner + "." + name] = int(mod.linear_out(torch.zeros(1, 1, mod.gru.hidden_size)).shape[-1])
            mods.append((owner + "." + name, mod))
    except (AttributeError, RuntimeError, TypeError):
        return False
    for key, mod in mods:
        mod.forward = types.MethodType(_live_gru_forward(key), mod)
    model._skkkk_live_dims = dims
    return True


class _LiveFilter:
    """Шумодав одной дорожки потоком: STFT, нормировки признаков и память GRU живут между кусками, поэтому
    результат совпадает с обработкой всей записи разом. Цена — задержка 30 мс (STFT 10 + заглядывание модели 20).

    Прежний путь (`_enhance`: каждый кусок 80 мс как отдельная запись) терял 4–6 дБ SI-SDR речи: libdf по умолчанию
    сбрасывает буферы STFT на каждом вызове, GRU и нормировки стартовали с нуля, конец куска не видел будущего."""

    def __init__(self, model, enhance):
        import numpy as np
        from df.model import ModelParams
        from df.utils import get_norm_alpha
        from libdf import unit_norm_init

        self.model = model
        self.enhance = enhance
        self.live = _install_live_grus(model)
        self.df = _new_df_state()
        if not self.live:
            return
        p = ModelParams()
        self.hop = p.hop_size
        self.nb_df = p.nb_df
        self.lookahead = max(p.conv_lookahead, p.df_lookahead)
        self.alpha = get_norm_alpha(False)
        self.erb_fb = self.df.erb_widths()
        # начальные состояния нормировок — те же, что libdf берёт без state
        self.erb_state = np.linspace(-60.0, -90.0, p.nb_erb, dtype=np.float32).reshape(1, -1)
        self.unit_state = unit_norm_init(p.nb_df).astype(np.float32)
        self.spec = np.zeros((1, 0, p.fft_size // 2 + 1), np.complex64)
        self.erb = np.zeros((1, 0, p.nb_erb), np.float32)
        self.unit = np.zeros((1, 0, p.nb_df), np.complex64)
        self.total = 0
        self.emitted = 0
        self.start = 0
        self.pending = np.zeros(0, np.float32)
        self.h: dict = {}

    def _features(self, spec):
        import numpy as np
        from libdf import erb, erb_norm, unit_norm

        a = self.alpha
        e_db = erb(spec, self.erb_fb)
        feat_erb = erb_norm(e_db.copy(), a, self.erb_state)  # erb_norm правит вход на месте
        feat_unit = unit_norm(spec[..., : self.nb_df], a, self.unit_state)
        s = self.erb_state[0].copy()
        u = self.unit_state[0].copy()
        mag = np.abs(spec[0, :, : self.nb_df])
        for t in range(spec.shape[1]):  # libdf не возвращает state — продолжаем его сами той же формулой
            s = e_db[0, t] * (1 - a) + s * a
            u = mag[t] * (1 - a) + u * a
        self.erb_state = s.reshape(1, -1).astype(np.float32)
        self.unit_state = u.reshape(1, -1).astype(np.float32)
        return feat_erb.astype(np.float32), feat_unit.astype(np.complex64)

    def process(self, pcm, atten=None):
        """int16 внутрь — int16 наружу (длина кратна 10 мс; на первом куске на 20 мс короче)."""
        import numpy as np
        import torch
        from df.utils import as_complex, as_real

        if atten is None:
            atten = _ATTEN
        if not self.live:
            return _enhance(self.model, self.df, self.enhance, pcm, atten)
        x = np.concatenate([self.pending, pcm.astype(np.float32) / 32768.0])
        n = len(x) // self.hop * self.hop
        self.pending = x[n:]
        if n == 0:
            return np.zeros(0, np.int16)
        spec = self.df.analysis(x[:n].reshape(1, -1), False)  # False: не сбрасывать буферы STFT между кусками
        feat_erb, feat_unit = self._features(spec)
        self.spec = np.concatenate([self.spec, spec], 1)
        self.erb = np.concatenate([self.erb, feat_erb], 1)
        self.unit = np.concatenate([self.unit, feat_unit], 1)
        self.total += spec.shape[1]
        end = self.total - self.lookahead
        if end <= self.emitted:
            return np.zeros(0, np.int16)
        first = max(self.start, self.emitted - LIVE_CONTEXT)
        lo = first - self.start
        c = self.emitted - first
        split = end - first
        spec_t = as_real(torch.as_tensor(self.spec[:, lo:]).unsqueeze(1))
        erb_t = torch.as_tensor(self.erb[:, lo:]).unsqueeze(1)
        unit_t = as_real(torch.as_tensor(self.unit[:, lo:]).unsqueeze(1))
        _LIVE_WINDOW["now"] = {"c": c, "split": split, "h": self.h, "dims": self.model._skkkk_live_dims}
        try:
            with torch.no_grad():
                enhanced = self.model(spec_t.clone(), erb_t, unit_t)[0]
        finally:
            _LIVE_WINDOW.pop("now", None)
        enh = as_complex(enhanced[:, 0, c:split])
        if atten is not None and abs(atten) > 0:
            lim = 10 ** (-abs(atten) / 20)
            enh = as_complex(spec_t[:, 0, c:split]) * lim + enh * (1 - lim)
        out = self.df.synthesis(np.ascontiguousarray(enh.numpy()), False)
        self.emitted = end
        keep = self.total - LIVE_CONTEXT - self.lookahead - 2
        if keep > self.start:
            cut = keep - self.start
            self.spec = self.spec[:, cut:]
            self.erb = self.erb[:, cut:]
            self.unit = self.unit[:, cut:]
            self.start = keep
        wave = np.clip(np.asarray(out, np.float32).reshape(-1), -1.0, 1.0)
        return (wave * 32767.0).astype(np.int16)


def frames_to_drop(queued: int, limit: int = BACKLOG_FRAMES, keep: int = BACKLOG_KEEP) -> int:
    return queued - keep if queued > limit else 0


def _drop_queued(stream, count: int) -> int:
    """Выбросить count самых старых кадров из очереди AudioStream (не трогая маркер конца потока None)."""
    items = getattr(getattr(stream, "_queue", None), "_queue", None)
    dropped = 0
    while items and dropped < count and items[0] is not None:
        items.popleft()
        dropped += 1
    return dropped


def release_slot(key: str, task) -> None:
    """Место под очистку освобождается сразу при отмене, не дожидаясь конца задачи: задача, уничтоженная
    недоделанной ("Task was destroyed but it is pending"), колбэк не вызывает — место висело бы вечно."""
    if _BUSY.get(key) is task:
        _BUSY.pop(key, None)


def release_room_slots(room_name: str) -> None:
    for key in [key for key in _BUSY if key.startswith(room_name + "|")]:
        _BUSY.pop(key, None)


def _queued_frames(stream) -> int:
    items = getattr(getattr(stream, "_queue", None), "_queue", None)
    try:
        return sum(1 for item in items if item is not None)
    except TypeError:
        return 0


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
        rec.write_manifest_merged(dest, manifest)

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
    local = None
    stream = None
    need = SAMPLE_RATE * CHUNK_MS // 1000
    chunks: list = []
    stats = {"since": time.monotonic(), "work": 0.0, "peak": 0.0, "chunks": 0, "dropped": 0, "queued": 0}
    try:
        try:
            await asyncio.wait_for(_unpublish_named(room, name), timeout=3.0)
        except asyncio.TimeoutError:
            pass
        source = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=QUEUE_MS)
        local = rtc.LocalAudioTrack.create_audio_track(name, source)
        try:
            await asyncio.wait_for(
                room.local_participant.publish_track(
                    local,
                    rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
                ),
                timeout=PUBLISH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            # слушатели этого человека остаются на сыром звуке (клиент играет его, пока нет clean-*)
            print("voice_cleaner: publish timeout", participant.identity, flush=True)
            return
        stream = rtc.AudioStream(
            track, sample_rate=SAMPLE_RATE, num_channels=1, frame_size_ms=FRAME_MS, capacity=STREAM_CAP_FRAMES
        )
        async for event in stream:
            if not wanted():
                return
            chunks.append(_pcm16(event.frame))
            if sum(len(item) for item in chunks) < need:
                continue
            pcm = np.concatenate(chunks)
            chunks.clear()
            async with lock:
                started = time.monotonic()
                clean = state.process(pcm, _ATTEN)
                spent = time.monotonic() - started
            stats["work"] += spent
            stats["peak"] = max(stats["peak"], spent)
            stats["chunks"] += 1
            if len(clean):
                await _emit(source, rtc, clean)
            queued = _queued_frames(stream)
            stats["queued"] = max(stats["queued"], queued)
            stats["dropped"] += _drop_queued(stream, frames_to_drop(queued)) * FRAME_MS
            if time.monotonic() - stats["since"] >= STATS_EVERY_S:
                print(
                    "voice_cleaner: pump", participant.identity,
                    "chunk_ms avg %.1f max %.1f" % (stats["work"] / max(1, stats["chunks"]) * 1000, stats["peak"] * 1000),
                    "queued_max", stats["queued"], "dropped_ms", stats["dropped"], "busy", len(_BUSY), flush=True,
                )
                stats.update(since=time.monotonic(), work=0.0, peak=0.0, chunks=0, dropped=0, queued=0)
    finally:
        # Комната могла уже закрыться: без таймаута задача повисала здесь и уничтожалась недоделанной.
        # Отмена могла прийти и посреди публикации — тогда снимаем то, что успело опубликоваться.
        steps = []
        if stream is not None:
            steps.append(lambda: stream.aclose())
        if local is not None:
            steps.append(lambda: room.local_participant.unpublish_track(local.sid))
        for step in steps:
            try:
                await asyncio.wait_for(step(), timeout=2.0)
            except BaseException:  # noqa: BLE001 — отмена/таймаут/ошибка SDK: дорожку всё равно отпускаем
                continue


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
            release_slot(room_name + "|" + identity, task)

    def start(track, participant) -> None:
        identity = participant.identity
        if not _is_human(identity):
            return
        if not person_wants(participant):
            cancel(identity)
            return
        running = pumps.get(identity)
        track_sid = getattr(track, "sid", "") or ""
        if running is not None and not running.done() and track_sid and getattr(running, "skkkk_track", "") == track_sid:
            return  # переключатель приходит дважды (данные + метаданные): не рвём только что начатую публикацию
        cancel(identity)
        key = room_name + "|" + identity
        if key not in _BUSY and len(_BUSY) >= _MAX_TRACKS:
            print("voice_cleaner: busy", len(_BUSY), "tracks, no clean track for", identity, flush=True)
            return
        task = asyncio.create_task(
            _pump(
                room,
                track,
                participant,
                model,
                _LiveFilter(model, enhance),
                enhance,
                lock,
                lambda: person_wants(participant),
            )
        )
        task.skkkk_track = track_sid
        pumps[identity] = task
        _BUSY[key] = task
        task.add_done_callback(lambda done, k=key: release_slot(k, done))

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
        for identity in list(pumps):
            cancel(identity)
        release_room_slots(room_name)
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
                    # Не hidden: LiveKit прячет от остальных и скрытого участника, и его дорожки — очищенный звук
                    # clean-* тогда не доходил ни до кого. Плитку и строку в списке клиент и так не рисует (isHidden).
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


def _warm_up(model, enhance) -> None:
    """Первый прогон модели идёт секунды (инициализация torch) — делаем его до комнат, а не на живой дорожке."""
    import numpy as np

    warm = _LiveFilter(model, enhance)
    for _ in range(4):
        warm.process(np.zeros(SAMPLE_RATE * CHUNK_MS // 1000, np.int16), _ATTEN)


async def _loop() -> None:
    key = _env("LIVEKIT_API_KEY")
    secret = _env("LIVEKIT_API_SECRET")
    if not key or not secret:
        print("voice_cleaner: no LiveKit keys", file=sys.stderr)
        return
    global _ATTEN, _MAX_TRACKS
    _ATTEN = atten_lim_db(_env(ATTEN_ENV) or None)
    _MAX_TRACKS = max_tracks(_env(MAX_TRACKS_ENV))
    model, enhance = _load_filter()
    lock = asyncio.Lock()
    _warm_up(model, enhance)
    print("voice_cleaner: DeepFilterNet CPU ready", "atten", _ATTEN, "live", _install_live_grus(model), "max_tracks", _MAX_TRACKS, flush=True)
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
            asyncio.create_task(_watch(name, join_jwt, model, enhance, lock, active, backoff))
        await asyncio.sleep(POLL_SEC)


async def _watch(name: str, token: str, model, enhance, lock, active: set[str], backoff=None) -> None:
    try:
        await _join(INTERNAL_URL, token, model, enhance, lock, name)
    except Exception as exc:
        print("voice_cleaner:", name, exc, file=sys.stderr)
    finally:
        active.discard(name)
        if backoff is not None:
            backoff.finished(name)


def main() -> int:
    if os.environ.get("MEETING_VOICE_SERVER", "1").strip() in {"0", "false", "off"}:
        return 0
    # одна копия на машину: вторая с тем же именем участника выбивала бы первую из комнаты
    instance = _recordings().acquire_single_instance(_data_dir() / "locks" / "voice_cleaner.instance")
    if instance is None:
        print("voice_cleaner: уже запущен, эта копия выходит", flush=True)
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
