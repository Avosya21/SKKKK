"""Путь звука и видео созвона в настоящем браузере: от издателя через LiveKit и шумодав `skkkk-dfn` к слушателю.

Гость А подаёт в фейковый микрофон речь с шумом (гул + щелчки клавиатуры, первые 3 с — только шум) и включает камеру,
гость Б слушает. Встреча создаётся кодом приложения в песочнице (проект SMOKE_PROJECT_ID, по умолчанию 29).

Проверяет:
- сервер: что публикует А (микрофон, слои камеры), что публикует шумодав (`clean-<identity>`), растёт ли сырая WAV-дорожка;
- у Б: для А играет ровно одна дорожка — очищенная; сколько шума срезано (дБ) при сохранённой речи; задержка шумодава;
  потери/джиттер/маскировка звука; разрешение и кадры камеры А;
- переключатель «Шумодав» у А: Б переходит на сырой звук и обратно без провала тишины;
- шумодав на минуту выпал из комнаты (его выкидывает сервер): Б не остаётся без звука А, запись дорожки продолжается;
- показ экрана: один слой, ~5 кадров/с.

Запуск (нужен venv с playwright и ffmpeg в PATH):  venv\\Scripts\\python.exe scripts\\qa_media_path.py [--base https://127.0.0.1:777]
Код выхода 0 — всё прошло. Встреча в конце завершается. Не запускать во время живого созвона (scripts/live_meetings.py)."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from playwright.sync_api import sync_playwright  # noqa: E402
from qa_room import check, end_meeting, join, make_meeting, wait_until, FAILS  # noqa: E402

SR = 16000
LEAD_S, TAIL_S = 3.0, 2.0
CLEANER_ID = "skkkk-dfn"

HOOK_B = """() => { const LK = window.LivekitClient; const r = SKKKKMeeting.room; window.__ev = []; window.__route = [];
  const push = (ev, p, pub, track) => window.__ev.push({t: Math.round(performance.now()), ev, who: p && p.identity,
    name: pub && (pub.trackName || ''), kind: track && track.kind});
  r.on(LK.RoomEvent.TrackSubscribed, (tr, pub, p) => push('sub', p, pub, tr));
  r.on(LK.RoomEvent.TrackUnsubscribed, (tr, pub, p) => push('unsub', p, pub, tr));
  r.on(LK.RoomEvent.ParticipantConnected, (p) => push('join', p));
  r.on(LK.RoomEvent.ParticipantDisconnected, (p) => push('leave', p));
  window.__who = (name) => { let id = null; r.remoteParticipants.forEach((p) => { if (p.name === name) id = p.identity; }); return id; };
  window.__pubs = (id) => { const out = {raw: null, clean: null}; const cleanName = 'clean-' + String(id).replace(/:/g, '.');
    r.remoteParticipants.forEach((p) => p.trackPublications.forEach((pub) => {
      if (pub.kind !== 'audio') return;
      if (p.identity === id && String(pub.source).indexOf('screen') < 0) out.raw = pub;
      if (p.identity === '""" + CLEANER_ID + """' && pub.trackName === cleanName) out.clean = pub; }));
    return out; };
  const playing = (pub) => Boolean(pub && pub.track && pub.track.attachedElements.some((el) => el.isConnected && !el.paused && !el.muted));
  window.__state = (id) => { const x = window.__pubs(id); const a = playing(x.raw), b = playing(x.clean);
    return a && b ? 'both' : a ? 'raw' : b ? 'clean' : 'none'; };
  // сколько элементов одновременно играют голос человека: сырой + все clean-* с его именем (их может быть две)
  window.__voices = (id) => { const cname = 'clean-' + String(id).replace(/:/g, '.'); let n = 0;
    r.remoteParticipants.forEach((p) => p.trackPublications.forEach((pub) => {
      if (pub.kind !== 'audio' || !pub.track || String(pub.source).indexOf('screen') >= 0) return;
      if (p.identity === id || (p.identity === '""" + CLEANER_ID + """' && pub.trackName === cname))
        n += pub.track.attachedElements.filter((el) => el.isConnected && !el.paused && !el.muted).length; }));
    return n; };
  window.__dup = [];
  window.__watch = (id) => { let last = null; setInterval(() => { const s = window.__state(id);
    const n = window.__voices(id); if (n > 1) window.__dup.push([Math.round(performance.now()), n]);
    if (s !== last) { window.__route.push([Math.round(performance.now()), s]); last = s; } }, 40); };
}"""

CAPTURE_B = """async ([id, secs]) => { const x = window.__pubs(id);
  if (!x.raw || !x.raw.track || !x.clean || !x.clean.track) return {error: 'нет дорожек', raw: !!x.raw, clean: !!x.clean};
  const ctx = new AudioContext({sampleRate: 16000}); await ctx.resume();
  const sink = ctx.createGain(); sink.gain.value = 0; sink.connect(ctx.destination);
  const keep = [];
  const tap = (mst) => { const el = new Audio(); el.muted = true; el.srcObject = new MediaStream([mst]); el.play().catch(() => 0); keep.push(el);
    const src = ctx.createMediaStreamSource(new MediaStream([mst])); const node = ctx.createScriptProcessor(4096, 1, 1);
    const rec = {chunks: [], t0: null}; node.onaudioprocess = (e) => { if (rec.t0 === null) rec.t0 = e.playbackTime;
      rec.chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); }; src.connect(node); node.connect(sink); rec.node = node; return rec; };
  const raw = tap(x.raw.track.mediaStreamTrack), clean = tap(x.clean.track.mediaStreamTrack);
  await new Promise((ok) => setTimeout(ok, secs * 1000));
  raw.node.disconnect(); clean.node.disconnect(); keep.forEach((el) => { el.srcObject = null; }); await ctx.close();
  const pack = (rec) => { const n = rec.chunks.reduce((s, c) => s + c.length, 0); const pcm = new Int16Array(n); let o = 0;
    rec.chunks.forEach((c) => { for (let i = 0; i < c.length; i++) pcm[o++] = Math.max(-32768, Math.min(32767, Math.round(c[i] * 32767))); });
    const bytes = new Uint8Array(pcm.buffer); let s = ''; for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return {b64: btoa(s), t0: rec.t0}; };
  return {raw: pack(raw), clean: pack(clean)}; }"""

STATS_B = """async (id) => { const r = SKKKKMeeting.room; const x = window.__pubs(id); const out = {};
  const rtp = async (track, kind) => { if (!track || !track.receiver) return null; const rep = await track.receiver.getStats(); let row = null; const codecs = {};
    rep.forEach((s) => { if (s.type === 'codec') codecs[s.id] = s.mimeType; });
    rep.forEach((s) => { if (s.type === 'inbound-rtp' && s.kind === kind) row = Object.assign({}, s, {codec: codecs[s.codecId]}); }); return row; };
  const pick = (s, keys) => s ? Object.fromEntries(keys.map((k) => [k, s[k]])) : null;
  const A = ['codec', 'packetsReceived', 'packetsLost', 'jitter', 'jitterBufferDelay', 'jitterBufferEmittedCount', 'concealedSamples', 'totalSamplesReceived', 'audioLevel', 'bytesReceived'];
  out.raw = pick(await rtp(x.raw && x.raw.track, 'audio'), A);
  out.clean = pick(await rtp(x.clean && x.clean.track, 'audio'), A);
  let cam = null, screen = null; r.remoteParticipants.forEach((p) => { if (p.identity !== id) return; p.trackPublications.forEach((pub) => {
    if (pub.kind !== 'video') return; if (String(pub.source).indexOf('screen') >= 0) screen = pub; else cam = pub; }); });
  const V = ['codec', 'frameWidth', 'frameHeight', 'framesPerSecond', 'framesDecoded', 'framesDropped', 'freezeCount', 'totalFreezesDuration', 'packetsReceived', 'packetsLost', 'nackCount', 'pliCount', 'decoderImplementation', 'bytesReceived'];
  out.cam = pick(await rtp(cam && cam.track, 'video'), V);
  out.screen = pick(await rtp(screen && screen.track, 'video'), V);
  return out; }"""

STATS_A = """async () => { const lp = SKKKKMeeting.room.localParticipant; const out = {};
  for (const pub of lp.trackPublications.values()) { const tr = pub.track; if (!tr || !tr.sender) continue; const rep = await tr.sender.getStats(); const codecs = {};
    rep.forEach((s) => { if (s.type === 'codec') codecs[s.id] = s.mimeType; }); const rows = [];
    rep.forEach((s) => { if (s.type !== 'outbound-rtp') return; rows.push({rid: s.rid || '', codec: codecs[s.codecId], w: s.frameWidth, h: s.frameHeight,
      fps: s.framesPerSecond, limit: s.qualityLimitationReason, target: s.targetBitrate, active: s.active, bytes: s.bytesSent}); });
    out[String(pub.source)] = rows; }
  return out; }"""


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def read_pcm(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def write_pcm(path: Path, x: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def make_signal(tmp: Path) -> tuple[Path, np.ndarray, np.ndarray]:
    """48 кГц для фейкового микрофона А + 16 кГц эталоны: вся смесь и чистая речь."""
    ffmpeg("-i", str(ROOT / "scripts" / "qa_data_speech_a.wav"), "-ar", "48000", "-ac", "1", str(tmp / "speech48.wav"))
    speech = read_pcm(tmp / "speech48.wav")
    speech = speech / (np.max(np.abs(speech)) + 1e-9) * 0.5
    sr = 48000
    clean = np.concatenate([np.zeros(int(LEAD_S * sr), np.float32), speech, np.zeros(int(TAIL_S * sr), np.float32)])
    rng = np.random.default_rng(7)
    n = len(clean)
    walk = np.cumsum(rng.standard_normal(n)).astype(np.float32)
    walk -= np.convolve(walk, np.ones(4801) / 4801, mode="same").astype(np.float32)
    fan = walk / (np.std(walk) + 1e-9) + 0.5 * np.sin(2 * np.pi * 100 * np.arange(n) / sr).astype(np.float32)
    keys = np.zeros(n, np.float32)
    for pos in rng.integers(0, n - 600, size=n // (sr // 5)):
        keys[pos:pos + 600] += rng.standard_normal(600).astype(np.float32) * np.exp(-np.arange(600) / 80).astype(np.float32)
    rms = lambda v: float(np.sqrt(np.mean(np.square(v)))) + 1e-9  # noqa: E731
    noise = fan / rms(fan) * rms(speech) * 10 ** (-5 / 20) + keys / rms(keys) * rms(speech) * 10 ** (-12 / 20)
    mix = clean + noise
    mix = mix / max(1.0, float(np.max(np.abs(mix))) / 0.95)
    write_pcm(tmp / "a_mic.wav", mix, sr)
    write_pcm(tmp / "a_ref_clean.wav", clean, sr)
    ffmpeg("-i", str(tmp / "a_mic.wav"), "-ar", str(SR), str(tmp / "a_mic16.wav"))
    ffmpeg("-i", str(tmp / "a_ref_clean.wav"), "-ar", str(SR), str(tmp / "a_ref16.wav"))
    return tmp / "a_mic.wav", read_pcm(tmp / "a_mic16.wav"), read_pcm(tmp / "a_ref16.wav")


def xcorr_lag(a: np.ndarray, b: np.ndarray, max_lag: int) -> tuple[int, float]:
    """Сдвиг b относительно a (b[t + lag] ~ a[t]), по FFT; вторым — нормированная корреляция."""
    n = len(a) + len(b)
    size = 1 << (n - 1).bit_length()
    fa = np.fft.rfft(a - a.mean(), size)
    fb = np.fft.rfft(b - b.mean(), size)
    cc = np.fft.irfft(np.conj(fa) * fb, size)
    lags = np.concatenate([np.arange(0, max_lag + 1), np.arange(-max_lag, 0)])
    vals = np.concatenate([cc[: max_lag + 1], cc[-max_lag:]])
    i = int(np.argmax(vals))
    norm = float(np.sqrt(np.dot(a - a.mean(), a - a.mean()) * np.dot(b - b.mean(), b - b.mean()))) + 1e-12
    return int(lags[i]), float(vals[i] / norm)


def level_db(x: np.ndarray) -> float:
    return 20 * np.log10(float(np.sqrt(np.mean(np.square(x)))) + 1e-9)


def _envelope(x: np.ndarray, hop: int = SR // 50) -> np.ndarray:
    n = len(x) // hop
    return 20 * np.log10(np.sqrt(np.mean(np.square(x[: n * hop].reshape(n, hop)), axis=1)) + 1e-5)


def _pct_db(x: np.ndarray, pct: float) -> float:
    hop = SR // 2
    n = len(x) // hop
    if n == 0:
        return -120.0
    env = 20 * np.log10(np.sqrt(np.mean(np.square(x[: n * hop].reshape(n, hop)), axis=1)) + 1e-6)
    return round(float(np.percentile(env, pct)), 1)


def analyse_audio(raw: np.ndarray, clean: np.ndarray, mix16: np.ndarray, ref16: np.ndarray) -> dict:
    """Окнами по 6 с: фейковый микрофон Chrome играет файл с уходом ~1.5 % от реального времени,
    поэтому к исходнику выравниваем каждое окно отдельно. Сырое и очищенное пишутся одним AudioContext у Б —
    сдвиг между ними постоянный, его и считаем задержкой шумодава."""
    period = len(mix16)
    loop = np.concatenate([mix16, mix16])
    win = 6 * SR
    clean = np.concatenate([clean, np.zeros(3 * SR, np.float32)])
    starts = list(range(0, len(raw) - win + 1, win))
    lags, weights = [], []
    for s0 in starts:
        lag, corr = xcorr_lag(raw[s0:s0 + win], clean[s0:s0 + win + 3 * SR], 3 * SR)
        lags.append(lag)
        weights.append(max(corr, 0.0))
    lag = int(np.median(lags)) if lags else 0
    # Где в исходнике каждое окно: окна почти из одного шума выравниваются по волне ненадёжно, поэтому ведём одну прямую
    # «позиция в исходнике = a + b·t» по окнам, согласным с медианой, и берём позицию из неё.
    found = []
    for s0 in starts:
        off, corr = xcorr_lag(raw[s0:s0 + win], loop, period - 1)
        found.append((s0, (off - s0) % period, corr))
    ref_d = float(np.median([d for _, d, _ in found])) if found else 0.0
    unwrap = [(s0, d + period * round((ref_d - d) / period), c) for s0, d, c in found]
    good = [(s0, d) for s0, d, _ in unwrap if abs(d - ref_d) <= 1.5 * SR]
    if len(good) >= 2:
        slope, base = np.polyfit([g[0] for g in good], [g[1] for g in good], 1)
    else:
        slope, base = 0.0, ref_d
    fit_ok = sum(1 for s0, d, _ in unwrap if abs(d - (base + slope * s0)) <= 0.5 * SR)
    noise_r, noise_c, speech_r, speech_c, env = [], [], [], [], {"raw": [], "clean": [], "ref": []}
    offsets = []
    for i, s0 in enumerate(starts):
        w_lag = lags[i] if i < len(lags) and weights[i] > 0.1 else lag  # задержка шумодава гуляет — выравниваем окно его сдвигом
        r = raw[s0:s0 + win]
        c = clean[s0 + w_lag:s0 + w_lag + win]
        if len(c) < win:
            break
        off = int(round(base + slope * s0 + s0)) % period
        offsets.append(off / SR)
        pos = (np.arange(win) + np.round(np.arange(win) * slope).astype(int) + off) % period
        ref = ref16[pos]
        noise = ((pos >= int(0.4 * SR)) & (pos < int((LEAD_S - 0.3) * SR))) | (pos >= period - int((TAIL_S - 0.3) * SR))
        in_speech = (pos >= int(LEAD_S * SR)) & (pos < period - int(TAIL_S * SR))
        loud = np.repeat(_envelope(ref) > -30, SR // 50)
        loud = np.concatenate([loud, np.zeros(win - len(loud), bool)])
        noise_r.append(r[noise])
        noise_c.append(c[noise])
        speech_r.append(r[in_speech & loud])
        speech_c.append(c[in_speech & loud])
        if in_speech.mean() > 0.9:
            env["raw"].append(_envelope(r))
            env["clean"].append(_envelope(c))
            env["ref"].append(_envelope(ref))

    def cat(parts: list) -> np.ndarray:
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)

    def env_corr(key: str) -> float | None:
        vals = [float(np.corrcoef(a, b)[0, 1]) for a, b in zip(env[key], env["ref"]) if np.std(a) > 0 and np.std(b) > 0]
        return round(float(np.mean(vals)), 3) if vals else None

    nr, nc, sr_, sc = cat(noise_r), cat(noise_c), cat(speech_r), cat(speech_c)
    return {
        "cleaner_lag_ms": round(lag / SR * 1000, 1),
        "cleaner_lag_windows_ms": [round(item / SR * 1000) for item in lags],
        "source_offsets_s": [round(item, 2) for item in offsets],
        "source_fit": f"{fit_ok}/{len(found)}",
        "source_drift_pct": round(float(slope) * 100, 2),
        "noise_only_raw_db": round(level_db(nr), 1) if len(nr) else None,
        "noise_only_clean_db": round(level_db(nc), 1) if len(nc) else None,
        "speech_raw_db": round(level_db(sr_), 1) if len(sr_) else None,
        "speech_clean_db": round(level_db(sc), 1) if len(sc) else None,
        "speech_env_corr_raw": env_corr("raw"),
        "speech_env_corr_clean": env_corr("clean"),
        "noise_samples_s": round(len(nr) / SR, 1),
        # без выравнивания к исходнику: тихие и громкие полусекунды (10-й и 90-й перцентили) сырого и очищенного
        "quiet10_raw_db": _pct_db(raw, 10),
        "quiet10_clean_db": _pct_db(clean[max(lag, 0):], 10),
        "loud90_raw_db": _pct_db(raw, 90),
        "loud90_clean_db": _pct_db(clean[max(lag, 0):], 90),
        "speech_samples_s": round(len(sr_) / SR, 1),
    }


def _in_thread(factory):
    # playwright sync держит свой цикл событий в этом потоке — asyncio.run только в отдельном
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(factory())).result(timeout=30)


def lk_participants(room_name: str) -> list[dict]:
    from livekit import api

    from app.app.config import get_settings

    s = get_settings()

    async def run():
        client = api.LiveKitAPI(s.livekit_rtc_internal.replace("ws", "http", 1), s.livekit_api_key, s.livekit_api_secret)
        try:
            res = await client.room.list_participants(api.ListParticipantsRequest(room=room_name))
            rows = []
            for p in res.participants:
                tracks = []
                for t in p.tracks:
                    tracks.append({
                        "name": t.name, "type": api.TrackType.Name(t.type), "source": api.TrackSource.Name(t.source),
                        "mime": t.mime_type, "muted": t.muted, "simulcast": t.simulcast,
                        "layers": [(api.VideoQuality.Name(l.quality), l.width, l.height) for l in t.layers],
                    })
                rows.append({"identity": p.identity, "name": p.name, "tracks": tracks})
            return rows
        finally:
            await client.aclose()

    return _in_thread(run)


def lk_remove(room_name: str, identity: str) -> None:
    from livekit import api

    from app.app.config import get_settings

    s = get_settings()

    async def run():
        client = api.LiveKitAPI(s.livekit_rtc_internal.replace("ws", "http", 1), s.livekit_api_key, s.livekit_api_secret)
        try:
            await client.room.remove_participant(api.RoomParticipantIdentity(room=room_name, identity=identity))
        finally:
            await client.aclose()

    _in_thread(run)


def track_files(room_name: str) -> dict[str, int]:
    from app.app.config import get_settings
    from app.app.services import meeting_recordings as mr

    folder = mr.room_dir(Path(get_settings().data_dir), room_name) / mr.TRACKS_DIRNAME
    if not folder.exists():
        return {}
    return {p.name: p.stat().st_size for p in folder.iterdir() if p.is_file()}


def route_summary(route: list, t_from: float, t_to: float) -> dict:
    """Сколько мс в окне у Б для А играло: raw / clean / both / none."""
    out = {"raw": 0.0, "clean": 0.0, "both": 0.0, "none": 0.0, "max_none_ms": 0.0}
    for i, (t, state) in enumerate(route):
        end = route[i + 1][0] if i + 1 < len(route) else t_to
        a, b = max(t, t_from), min(end, t_to)
        if b > a:
            out[state] += b - a
            if state == "none":
                out["max_none_ms"] = max(out["max_none_ms"], b - a)
    return {k: round(v) for k, v in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:777")
    ap.add_argument("--capture", type=int, default=0, help="секунд записи у Б (0 — один круг сигнала)")
    ap.add_argument("--report", default="", help="куда сохранить JSON-отчёт")
    args = ap.parse_args()
    report: dict = {}
    tmp = Path(tempfile.mkdtemp(prefix="qa_media_"))
    mic48, mix16, ref16 = make_signal(tmp)
    capture_s = args.capture or int(len(mix16) / SR) + 8  # круг сигнала + запас: окна должны захватить паузу «только шум»
    db, meeting, token = make_meeting()
    room_name = meeting.room_name
    print(f"встреча #{meeting.id}, комната {room_name}", flush=True)
    try:
        with sync_playwright() as p:
            common = ["--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required",
                      "--auto-select-desktop-capture-source=Entire screen", "--enable-usermedia-screen-capturing"]
            br_a = p.chromium.launch(headless=True, args=["--use-fake-device-for-media-stream",
                                                          f"--use-file-for-fake-audio-capture={mic48.as_posix()}", *common])
            br_b = p.chromium.launch(headless=True, args=["--use-fake-device-for-media-stream", *common])
            ctx_opts = dict(ignore_https_errors=True, permissions=["microphone", "camera"], viewport={"width": 1280, "height": 800})
            b = br_b.new_context(**ctx_opts).new_page()
            join(b, args.base, token, "Гость Б")
            b.click("#meeting-mic")  # Б молчит: слушаем только А
            b.evaluate(HOOK_B)
            a = br_a.new_context(**ctx_opts).new_page()
            t_join = time.time()
            join(a, args.base, token, "Гость А")
            a.click("#meeting-cam")
            ida = wait_until(lambda: b.evaluate("window.__who('Гость А')"), 20)
            check(bool(ida), f"Б видит гостя А ({ida})")
            b.evaluate("(id) => window.__watch(id)", ida)
            got = wait_until(lambda: b.evaluate("(id) => { const x = window.__pubs(id); return !!(x.clean && x.clean.track); }", ida), 25)
            clean_after = time.time() - t_join
            check(bool(got), f"у Б появилась очищенная дорожка А (через {clean_after:.1f} с после входа А)")
            ev = b.evaluate("window.__ev")
            t_raw = next((e["t"] for e in ev if e["ev"] == "sub" and e["who"] == ida and e["kind"] == "audio"), None)
            t_clean = next((e["t"] for e in ev if e["ev"] == "sub" and e["who"] == CLEANER_ID and e["name"].startswith("clean-")
                            and e["name"] == "clean-" + ida.replace(":", ".")), None)
            if t_raw is not None and t_clean is not None:
                report["clean_after_raw_ms"] = t_clean - t_raw
                print(f"  сырая дорожка А у Б → очищенная через {t_clean - t_raw} мс", flush=True)
            time.sleep(4)
            route = b.evaluate("window.__route")
            if t_raw is not None and t_clean is not None:
                join_gap = route_summary(route, t_raw, t_clean + 400)
                report["join_route"] = join_gap
                check(join_gap["max_none_ms"] < 600, f"пока шумодав готовит дорожку, А у Б не пропадает (тишина {join_gap['max_none_ms']} мс)")

            # --- сервер: что опубликовано
            parts = lk_participants(room_name)
            report["server"] = parts
            by_id = {row["identity"]: row for row in parts}
            a_tracks = by_id.get(ida, {}).get("tracks", [])
            mic = [t for t in a_tracks if t["source"] == "MICROPHONE"]
            cam = [t for t in a_tracks if t["source"] == "CAMERA"]
            check(bool(mic) and (mic[0]["mime"] or "").lower() in ("audio/opus", "audio/red"),
                  f"сервер: микрофон А — {mic[0]['mime'] if mic else 'нет'} (RED = Opus с резервом от потерь)")
            layers = cam[0]["layers"] if cam else []
            check(bool(cam) and len(layers) >= 2, f"сервер: камера А {cam[0]['mime'] if cam else 'нет'}, слои {layers}")
            dfn = by_id.get(CLEANER_ID, {}).get("tracks", [])
            check(any(t["name"] == "clean-" + ida.replace(":", ".") for t in dfn), f"сервер: шумодав публикует {[t['name'] for t in dfn]}")
            check("skkkk-rec" in by_id, "сервер: запись экрана (skkkk-rec) в комнате")
            files0 = track_files(room_name)
            t_files0 = time.time()

            # --- у Б: маршрут звука А и замер
            state = b.evaluate("(id) => window.__state(id)", ida)
            n_el = b.evaluate("(id) => { const x = window.__pubs(id); return [x.raw, x.clean].map((p) => p && p.track ? p.track.attachedElements.filter((e) => e.isConnected && !e.paused).length : -1); }", ida)
            check(state == "clean" and n_el[1] == 1 and n_el[0] == 0, f"у Б для А играет только очищенная дорожка (сырая/очищ. элементов {n_el})")
            print(f"  записываю у Б {capture_s} с сырого и очищенного звука А…", flush=True)
            cap = b.evaluate(CAPTURE_B, [ida, capture_s])
            if cap.get("error"):
                check(False, f"запись у Б: {cap}")
            else:
                raw = np.frombuffer(base64.b64decode(cap["raw"]["b64"]), dtype=np.int16).astype(np.float32) / 32768.0
                clean = np.frombuffer(base64.b64decode(cap["clean"]["b64"]), dtype=np.int16).astype(np.float32) / 32768.0
                write_pcm(tmp / "b_raw.wav", raw, SR)
                write_pcm(tmp / "b_clean.wav", clean, SR)
                res = analyse_audio(raw, clean, mix16, ref16)
                report["audio"] = res
                print("  звук у Б:", json.dumps(res, ensure_ascii=False), flush=True)
                fit_ok, fit_all = (int(x) for x in res["source_fit"].split("/"))
                check(fit_all > 0 and fit_ok >= 0.6 * fit_all,
                      f"сырой звук А у Б — тот, что подал А (окон на одной прямой {res['source_fit']}, уход темпа {res['source_drift_pct']} %)")
                # паузы и речь — по перцентилям полусекунд (не зависят от выравнивания к исходнику);
                # окна «только шум» по исходнику — справка: фейковый микрофон Chrome их иногда сдвигает на секунды
                cut = res["quiet10_raw_db"] - res["quiet10_clean_db"]
                check(cut >= 6, f"шумодав глушит паузы ещё на {cut:.1f} дБ поверх шумодава браузера "
                      f"(по окнам исходника {(res['noise_only_raw_db'] or 0) - (res['noise_only_clean_db'] or 0):.1f} дБ)")
                keep = res["loud90_clean_db"] - res["loud90_raw_db"]
                check(abs(keep) <= 6, f"громкость речи после шумодава: {keep:+.1f} дБ")
                # Основа ~0.2–0.3 с (кусок 80 мс, заглядывание 30 мс, лишний круг через SFU); заминки машины добавляют не больше
                # очереди входа (150 мс) и буфера выхода (120 мс). Раньше задержка копилась без предела (1.9 → 2.7 с).
                tail = res["cleaner_lag_windows_ms"][-3:]
                check(0 <= res["cleaner_lag_ms"] <= 550 and max(tail or [0]) <= 650,
                      f"задержка очищенного звука ограничена: медиана {res['cleaner_lag_ms']} мс (по окнам {res['cleaner_lag_windows_ms']})")
                # качество речи точно меряет офлайн-сверка (test_live_filter_matches_whole_recording); здесь — справка
                print(f"  огибающая речи ~ чистой речи: сырой {res['speech_env_corr_raw']}, очищенный {res['speech_env_corr_clean']}", flush=True)
            st_b = b.evaluate(STATS_B, ida)
            st_a = a.evaluate(STATS_A)
            report["stats_b"], report["stats_a"] = st_b, st_a
            for key in ("raw", "clean"):
                s = st_b.get(key) or {}
                recv = (s.get("packetsReceived") or 0) + (s.get("packetsLost") or 0)
                loss = (s.get("packetsLost") or 0) / recv * 100 if recv else 100.0
                conceal = (s.get("concealedSamples") or 0) / max(1, s.get("totalSamplesReceived") or 0) * 100
                jb = (s.get("jitterBufferDelay") or 0) / max(1, s.get("jitterBufferEmittedCount") or 0) * 1000
                print(f"  {key}: {s.get('codec')} потери {loss:.2f}%, джиттер {(s.get('jitter') or 0) * 1000:.1f} мс, "
                      f"буфер {jb:.0f} мс, маскировка {conceal:.2f}%", flush=True)
                check(loss < 2 and conceal < 5, f"звук А ({key}) у Б без потерь и провалов")
            camst = st_b.get("cam") or {}
            print(f"  камера А у Б: {camst}", flush=True)
            print(f"  у А отправка: {json.dumps(st_a, ensure_ascii=False)}", flush=True)
            check((camst.get("frameWidth") or 0) >= 320 and (camst.get("framesPerSecond") or 0) >= 10,
                  f"камера А у Б: {camst.get('frameWidth')}x{camst.get('frameHeight')} {camst.get('framesPerSecond')} к/с")
            seen_s = max(1.0, (camst.get("framesDecoded") or 0) / max(1, camst.get("framesPerSecond") or 1))
            frozen = (camst.get("totalFreezesDuration") or 0) / seen_s * 100
            check(frozen <= 5, f"камера А у Б почти без замираний ({camst.get('freezeCount')} шт., {frozen:.1f} % времени)")
            files1 = track_files(room_name)
            dt = time.time() - t_files0
            key_a = ida.replace(":", "_")
            grow = {k: (files1.get(k, 0) - files0.get(k, 0)) / dt for k in files1 if k.endswith(".wav")}
            report["record_bytes_per_s"] = {k: round(v) for k, v in grow.items()}
            a_rate = max((v for k, v in grow.items() if key_a in k or ida.split(":")[-1] in k), default=0)
            check(80000 <= a_rate <= 115000, f"сырая WAV-дорожка А пишется ({a_rate:.0f} Б/с, норма 96000)")

            # --- переключатель «Шумодав» у А
            t0 = b.evaluate("performance.now()")
            a.click("#meeting-denoise")
            ok = wait_until(lambda: b.evaluate("(id) => window.__state(id)", ida) == "raw", 8, 0.1)
            t1 = b.evaluate("performance.now()")
            check(bool(ok), f"шумодав выключен у А → Б слышит сырой звук А ({(t1 - t0):.0f} мс)")
            def clean_pubs_a() -> int:
                tracks = {r["identity"]: r for r in lk_participants(room_name)}.get(CLEANER_ID, {}).get("tracks", [])
                return sum(1 for t in tracks if t["name"] == "clean-" + ida.replace(":", "."))

            time.sleep(2)
            # clean-* живёт всё время, пока человек в комнате: переключатель меняет только то, что играет слушатель
            check(clean_pubs_a() == 1, f"сервер: у А одна clean-дорожка и при выключенном шумодаве ({clean_pubs_a()})")
            t2 = b.evaluate("performance.now()")
            a.click("#meeting-denoise")
            ok = wait_until(lambda: b.evaluate("(id) => window.__state(id)", ida) == "clean", 20, 0.1)
            t3 = b.evaluate("performance.now()")
            check(bool(ok), f"шумодав снова включён → Б слышит очищенный звук А ({(t3 - t2):.0f} мс)")
            route = b.evaluate("window.__route")
            sw = route_summary(route, t0, t3 + 300)
            report["toggle_route"] = sw
            check(sw["max_none_ms"] < 600 and sw["both"] < 300, f"при переключениях нет тишины и двойного звука ({sw})")

            # --- частые переключения и повторный вход: голос А у Б никогда не звучит дважды
            b.evaluate("window.__dup = []")
            for _ in range(4):
                a.click("#meeting-denoise")
                time.sleep(0.6)
            back_clean = wait_until(lambda: b.evaluate("(id) => window.__state(id)", ida) == "clean", 20, 0.2)
            time.sleep(3)
            dup = b.evaluate("window.__dup.length")
            check(bool(back_clean) and dup == 0, f"4 переключения подряд: голос А у Б одним потоком (двойных тиков {dup}), в конце очищенный")
            check(clean_pubs_a() == 1, f"сервер: после переключений у А по-прежнему одна clean-дорожка ({clean_pubs_a()})")
            n_audio = b.evaluate("document.querySelectorAll('#meeting-stage audio').length")
            check(n_audio == 1, f"у Б на странице один элемент звука на одного собеседника ({n_audio})")

            # --- шумодав выпал из комнаты
            time.sleep(2)
            t4 = b.evaluate("performance.now()")
            size_before = track_files(room_name)
            lk_remove(room_name, CLEANER_ID)
            back_raw = wait_until(lambda: b.evaluate("(id) => window.__state(id)", ida) == "raw", 6, 0.1)
            t5 = b.evaluate("performance.now()")
            check(bool(back_raw), f"шумодав выпал → Б переходит на сырой звук А ({(t5 - t4):.0f} мс)")
            back = wait_until(lambda: b.evaluate("(id) => window.__state(id)", ida) == "clean", 60, 0.2)
            t6 = b.evaluate("performance.now()")
            check(bool(back), f"шумодав вернулся → снова очищенный звук ({(t6 - t4) / 1000:.1f} с)")
            route = b.evaluate("window.__route")
            kick = route_summary(route, t4, t6 + 300)
            report["cleaner_kick_route"] = kick
            check(kick["max_none_ms"] < 1500, f"пока шумодава нет, А у Б не молчит ({kick})")
            time.sleep(3)
            size_after = track_files(room_name)
            shrunk = [k for k, v in size_before.items() if size_after.get(k, 0) < v]
            check(not shrunk, f"после переподключения шумодава записи не обрезаны ({len(size_after)} файлов)")
            wav_a = next((k for k in size_after if k.endswith(".wav") and ida.split(":")[-1] in k), "")
            time.sleep(3)
            grew = track_files(room_name).get(wav_a, 0) - size_after.get(wav_a, 0)
            check(grew > 3 * 60000, f"запись дорожки А после переподключения идёт дальше (+{grew} Б за 3 с)")

            # --- показ экрана
            a.click("#meeting-screen")
            has_screen = wait_until(lambda: b.locator(".meeting-tile-screen video").count() >= 1, 20)
            check(bool(has_screen), "у Б появилась плитка экрана А")
            time.sleep(8)
            st_b = b.evaluate(STATS_B, ida)
            scr = st_b.get("screen") or {}
            report["screen_b"] = scr
            parts = lk_participants(room_name)
            scr_srv = [t for row in parts if row["identity"] == ida for t in row["tracks"] if t["source"] == "SCREEN_SHARE"]
            report["screen_server"] = scr_srv
            print(f"  экран у Б: {scr}; на сервере: {scr_srv}", flush=True)
            check(bool(scr_srv) and len(scr_srv[0]["layers"]) <= 1, f"экран А на сервере одним слоем ({scr_srv[0]['layers'] if scr_srv else '—'})")
            check(1 <= (scr.get("framesPerSecond") or 0) <= 8 and (scr.get("frameWidth") or 0) >= 640,
                  f"экран у Б: {scr.get('frameWidth')}x{scr.get('frameHeight')} {scr.get('framesPerSecond')} к/с")
            errs_a = a.evaluate("window.__qaErrors || []") if a else []
            del errs_a
            br_a.close()
            br_b.close()
    finally:
        end_meeting(db, meeting)
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"файлы замера: {tmp}", flush=True)
    print("ИТОГ:", "всё прошло" if not FAILS else f"провалов {len(FAILS)}", flush=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
