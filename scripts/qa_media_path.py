"""Путь звука и видео созвона в настоящем браузере: шумодав DFN3 в браузере говорящего, сырая дорожка — рекордеру.

Гость А подаёт в фейковый микрофон речь с шумом (гул + щелчки клавиатуры, первые 3 с — только шум) и включает камеру,
гость Б слушает. Встреча создаётся кодом приложения в песочнице (проект SMOKE_PROJECT_ID, по умолчанию 29).

Проверяет:
- у А: на микрофоне стоит процессор DFN3, рядом опубликована сырая дорожка `skkkk-raw`;
- сервер: что публикует А (микрофон, сырая, слои камеры); рекордер голоса `skkkk-dfn` в комнате; WAV-дорожка А растёт
  и в ней сырой звук (паузы громче, чем слышит Б);
- у Б: для А играет ровно одна дорожка (очищенная), на сырую Б не подписан; сколько шума срезано (дБ) при сохранённой
  речи; потери/джиттер/маскировка звука; разрешение и кадры камеры А;
- переключатель «Шумодав» у А: у Б тот же один поток, паузы становятся громче и снова тише, без тишины и двойного звука;
- рекордер выпал из комнаты (его выкидывает сервер): у Б звук А не меняется, запись дорожки продолжается без обрезки;
- показ экрана: один слой, ~5 кадров/с.

Запуск (нужен venv с playwright и ffmpeg в PATH):  venv\\Scripts\\python.exe scripts\\qa_media_path.py [--base https://127.0.0.1:777]
Код выхода 0 — всё прошло. Встреча в конце завершается. Не запускать во время живого созвона (scripts/live_meetings.py)."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
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
RECORDER_ID = "skkkk-dfn"
RAW_TRACK_NAME = "skkkk-raw"

HOOK_B = """() => { const LK = window.LivekitClient; const r = SKKKKMeeting.room; window.__route = [];
  window.__who = (name) => { let id = null; r.remoteParticipants.forEach((p) => { if (p.name === name) id = p.identity; }); return id; };
  window.__pubs = (id) => { const out = {main: null, raw: null};
    r.remoteParticipants.forEach((p) => { if (p.identity !== id) return; p.trackPublications.forEach((pub) => {
      if (pub.kind !== 'audio' || String(pub.source).indexOf('screen') >= 0) return;
      if (pub.trackName === '""" + RAW_TRACK_NAME + """') out.raw = pub; else out.main = pub; }); });
    return out; };
  const playing = (pub) => pub && pub.track ? pub.track.attachedElements.filter((el) => el.isConnected && !el.paused && !el.muted).length : 0;
  window.__state = (id) => { const x = window.__pubs(id); const a = playing(x.main), b = playing(x.raw);
    return b ? 'raw' : a > 1 ? 'both' : a === 1 ? 'main' : 'none'; };
  window.__dup = [];
  window.__watch = (id) => { let last = null; setInterval(() => { const s = window.__state(id);
    if (s === 'both' || s === 'raw') window.__dup.push([Math.round(performance.now()), s]);
    if (s !== last) { window.__route.push([Math.round(performance.now()), s]); last = s; } }, 40); };
}"""

LOCAL_A = """() => { const out = []; SKKKKMeeting.room.localParticipant.trackPublications.forEach((pub) => {
  const t = pub.track; out.push({name: pub.trackName || '', source: String(pub.source), muted: pub.isMuted,
    processor: t && t.getProcessor && t.getProcessor() ? t.getProcessor().name : null}); }); return out; }"""

CAPTURE_B = """async ([id, secs]) => { const x = window.__pubs(id);
  if (!x.main || !x.main.track) return {error: 'нет дорожки А'};
  const ctx = new AudioContext({sampleRate: 16000}); await ctx.resume();
  const sink = ctx.createGain(); sink.gain.value = 0; sink.connect(ctx.destination);
  const mst = x.main.track.mediaStreamTrack; const el = new Audio(); el.muted = true; el.srcObject = new MediaStream([mst]); el.play().catch(() => 0);
  const src = ctx.createMediaStreamSource(new MediaStream([mst])); const node = ctx.createScriptProcessor(4096, 1, 1);
  const chunks = []; node.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  src.connect(node); node.connect(sink);
  await new Promise((ok) => setTimeout(ok, secs * 1000));
  node.disconnect(); el.srcObject = null; await ctx.close();
  const n = chunks.reduce((s, c) => s + c.length, 0); const pcm = new Int16Array(n); let o = 0;
  chunks.forEach((c) => { for (let i = 0; i < c.length; i++) pcm[o++] = Math.max(-32768, Math.min(32767, Math.round(c[i] * 32767))); });
  const bytes = new Uint8Array(pcm.buffer); let s = ''; for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return {b64: btoa(s)}; }"""

STATS_B = """async (id) => { const r = SKKKKMeeting.room; const x = window.__pubs(id); const out = {};
  const rtp = async (track, kind) => { if (!track || !track.receiver) return null; const rep = await track.receiver.getStats(); let row = null; const codecs = {};
    rep.forEach((s) => { if (s.type === 'codec') codecs[s.id] = s.mimeType; });
    rep.forEach((s) => { if (s.type === 'inbound-rtp' && s.kind === kind) row = Object.assign({}, s, {codec: codecs[s.codecId]}); }); return row; };
  const pick = (s, keys) => s ? Object.fromEntries(keys.map((k) => [k, s[k]])) : null;
  const A = ['codec', 'packetsReceived', 'packetsLost', 'jitter', 'jitterBufferDelay', 'jitterBufferEmittedCount', 'concealedSamples', 'totalSamplesReceived', 'audioLevel', 'bytesReceived'];
  out.main = pick(await rtp(x.main && x.main.track, 'audio'), A);
  out.raw_subscribed = Boolean(x.raw && x.raw.isSubscribed);
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
    out[(pub.trackName || '') + ':' + String(pub.source)] = rows; }
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


def make_signal(tmp: Path) -> tuple[Path, np.ndarray]:
    """48 кГц для фейкового микрофона А + 16 кГц эталон смеси (длина круга сигнала)."""
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
    ffmpeg("-i", str(tmp / "a_mic.wav"), "-ar", str(SR), str(tmp / "a_mic16.wav"))
    return tmp / "a_mic.wav", read_pcm(tmp / "a_mic16.wav")


def _pct_db(x: np.ndarray, pct: float, sr: int = SR) -> float:
    """Перцентиль громкости полусекунд: 10-й — паузы (шум), 90-й — речь. Не зависит от выравнивания к исходнику."""
    hop = sr // 2
    n = len(x) // hop
    if n == 0:
        return -120.0
    env = 20 * np.log10(np.sqrt(np.mean(np.square(x[: n * hop].reshape(n, hop)), axis=1)) + 1e-6)
    return round(float(np.percentile(env, pct)), 1)


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


def track_dir(room_name: str) -> Path:
    from app.app.config import get_settings
    from app.app.services import meeting_recordings as mr

    return mr.room_dir(Path(get_settings().data_dir), room_name) / mr.TRACKS_DIRNAME


def track_files(room_name: str) -> dict[str, int]:
    folder = track_dir(room_name)
    if not folder.exists():
        return {}
    return {p.name: p.stat().st_size for p in folder.iterdir() if p.is_file()}


def wav_tail(path: Path, seconds: float) -> np.ndarray:
    """Последние seconds записи рекордера (48 кГц) → 16 кГц для сравнения с тем, что слышит Б."""
    with wave.open(str(path), "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        w.setpos(max(0, n - int(seconds * sr)))
        x = np.frombuffer(w.readframes(int(seconds * sr)), dtype=np.int16).astype(np.float32) / 32768.0
    step = sr // SR
    return x[: len(x) // step * step].reshape(-1, step).mean(axis=1)


def route_summary(route: list, t_from: float, t_to: float) -> dict:
    """Сколько мс в окне у Б для А играло: main (одна очищенная) / both (две копии) / raw (сырая) / none."""
    out = {"main": 0.0, "both": 0.0, "raw": 0.0, "none": 0.0, "max_none_ms": 0.0}
    for i, (t, state) in enumerate(route):
        end = route[i + 1][0] if i + 1 < len(route) else t_to
        a, b = max(t, t_from), min(end, t_to)
        if b > a:
            out[state] += b - a
            if state == "none":
                out["max_none_ms"] = max(out["max_none_ms"], b - a)
    return {k: round(v) for k, v in out.items()}


def capture_b(b, ida: str, secs: int, tmp: Path, name: str) -> np.ndarray | None:
    cap = b.evaluate(CAPTURE_B, [ida, secs])
    if cap.get("error"):
        check(False, f"запись у Б: {cap}")
        return None
    x = np.frombuffer(base64.b64decode(cap["b64"]), dtype=np.int16).astype(np.float32) / 32768.0
    write_pcm(tmp / name, x, SR)
    return x


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:777")
    ap.add_argument("--capture", type=int, default=0, help="секунд записи у Б (0 — один круг сигнала)")
    ap.add_argument("--report", default="", help="куда сохранить JSON-отчёт")
    args = ap.parse_args()
    report: dict = {}
    tmp = Path(tempfile.mkdtemp(prefix="qa_media_"))
    mic48, mix16 = make_signal(tmp)
    capture_s = args.capture or int(len(mix16) / SR) + 8  # круг сигнала + запас: в окно попадает пауза «только шум»
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
            join(a, args.base, token, "Гость А")
            a.click("#meeting-cam")
            ida = wait_until(lambda: b.evaluate("window.__who('Гость А')"), 20)
            check(bool(ida), f"Б видит гостя А ({ida})")
            b.evaluate("(id) => window.__watch(id)", ida)

            # --- у А: DFN3 на микрофоне и сырая дорожка рядом
            local_a = wait_until(lambda: (lambda pubs: pubs if any(x["name"] == RAW_TRACK_NAME for x in pubs) else None)(a.evaluate(LOCAL_A)), 25)
            report["local_a"] = local_a
            mic_a = [x for x in (local_a or []) if x["source"] == "microphone"]
            check(bool(mic_a) and mic_a[0]["processor"] == "deepfilternet3-noise-filter",
                  f"у А на микрофоне шумодав DFN3 ({mic_a[0]['processor'] if mic_a else 'нет микрофона'})")
            check(any(x["name"] == RAW_TRACK_NAME for x in (local_a or [])), f"у А опубликована сырая дорожка для записи ({local_a})")
            time.sleep(3)

            # --- сервер: что опубликовано
            parts = lk_participants(room_name)
            report["server"] = parts
            by_id = {row["identity"]: row for row in parts}
            a_tracks = by_id.get(ida, {}).get("tracks", [])
            mic = [t for t in a_tracks if t["source"] == "MICROPHONE"]
            raw = [t for t in a_tracks if t["name"] == RAW_TRACK_NAME]
            cam = [t for t in a_tracks if t["source"] == "CAMERA"]
            check(bool(mic) and (mic[0]["mime"] or "").lower() in ("audio/opus", "audio/red"),
                  f"сервер: микрофон А — {mic[0]['mime'] if mic else 'нет'} (RED = Opus с резервом от потерь)")
            check(len(raw) == 1, f"сервер: у А одна сырая дорожка ({[t['name'] for t in a_tracks]})")
            layers = cam[0]["layers"] if cam else []
            check(bool(cam) and len(layers) >= 2, f"сервер: камера А {cam[0]['mime'] if cam else 'нет'}, слои {layers}")
            check(RECORDER_ID in by_id and not by_id[RECORDER_ID]["tracks"], "сервер: рекордер голоса (skkkk-dfn) в комнате и ничего не публикует")
            check("skkkk-rec" in by_id, "сервер: запись экрана (skkkk-rec) в комнате")
            files0 = track_files(room_name)
            t_files0 = time.time()

            # --- у Б: одна дорожка А, на сырую не подписан; сколько шума срезано
            state = b.evaluate("(id) => window.__state(id)", ida)
            st_b = b.evaluate(STATS_B, ida)
            check(state == "main" and not st_b.get("raw_subscribed"), f"у Б для А играет одна очищенная дорожка, сырую Б не качает ({state})")
            print(f"  записываю у Б {capture_s} с звука А…", flush=True)
            heard = capture_b(b, ida, capture_s, tmp, "b_heard.wav")
            wav_a = next((track_dir(room_name) / k for k in track_files(room_name) if k.endswith(".wav") and ida.split(":")[-1] in k), None)
            if heard is not None and wav_a is not None:
                recorded = wav_tail(wav_a, capture_s)
                write_pcm(tmp / "a_recorded.wav", recorded, SR)
                res = {
                    "quiet10_heard_db": _pct_db(heard, 10), "loud90_heard_db": _pct_db(heard, 90),
                    "quiet10_recorded_db": _pct_db(recorded, 10), "loud90_recorded_db": _pct_db(recorded, 90),
                }
                report["audio"] = res
                print("  звук:", json.dumps(res, ensure_ascii=False), flush=True)
                cut = res["quiet10_recorded_db"] - res["quiet10_heard_db"]
                check(cut >= 6, f"шумодав DFN3 глушит паузы на {cut:.1f} дБ (Б слышит тише, чем пишет рекордер)")
                keep = res["loud90_heard_db"] - res["loud90_recorded_db"]
                check(abs(keep) <= 6, f"громкость речи после шумодава: {keep:+.1f} дБ")
            else:
                check(False, f"нет записи у Б или WAV-дорожки А ({wav_a})")
            st_b = b.evaluate(STATS_B, ida)
            st_a = a.evaluate(STATS_A)
            report["stats_b"], report["stats_a"] = st_b, st_a
            s = st_b.get("main") or {}
            recv = (s.get("packetsReceived") or 0) + (s.get("packetsLost") or 0)
            loss = (s.get("packetsLost") or 0) / recv * 100 if recv else 100.0
            conceal = (s.get("concealedSamples") or 0) / max(1, s.get("totalSamplesReceived") or 0) * 100
            jb = (s.get("jitterBufferDelay") or 0) / max(1, s.get("jitterBufferEmittedCount") or 0) * 1000
            print(f"  звук А у Б: {s.get('codec')} потери {loss:.2f}%, джиттер {(s.get('jitter') or 0) * 1000:.1f} мс, "
                  f"буфер {jb:.0f} мс, маскировка {conceal:.2f}%", flush=True)
            check(loss < 2 and conceal < 5, "звук А у Б без потерь и провалов")
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
            grow = {k: (files1.get(k, 0) - files0.get(k, 0)) / dt for k in files1 if k.endswith(".wav")}
            report["record_bytes_per_s"] = {k: round(v) for k, v in grow.items()}
            a_rate = max((v for k, v in grow.items() if ida.split(":")[-1] in k), default=0)
            check(80000 <= a_rate <= 115000, f"WAV-дорожка А пишется ({a_rate:.0f} Б/с, норма 96000)")

            # --- переключатель «Шумодав» у А: тот же один поток, паузы громче и снова тише
            t0 = b.evaluate("performance.now()")
            a.click("#meeting-denoise")
            time.sleep(2)
            off = capture_b(b, ida, 8, tmp, "b_denoise_off.wav")
            a.click("#meeting-denoise")
            time.sleep(2)
            on = capture_b(b, ida, 8, tmp, "b_denoise_on.wav")
            t1 = b.evaluate("performance.now()")
            if off is not None and on is not None:
                lift = _pct_db(off, 10) - _pct_db(on, 10)
                report["toggle_quiet_db"] = {"off": _pct_db(off, 10), "on": _pct_db(on, 10)}
                check(lift >= 6, f"шумодав выкл → паузы у Б громче на {lift:.1f} дБ, вкл → снова тише")
            sw = route_summary(b.evaluate("window.__route"), t0, t1)
            report["toggle_route"] = sw
            check(sw["none"] == 0 and sw["both"] == 0 and sw["raw"] == 0, f"при переключениях у Б один поток А без тишины ({sw})")
            b.evaluate("window.__dup = []")
            for _ in range(4):
                a.click("#meeting-denoise")
                time.sleep(0.6)
            time.sleep(3)
            dup = b.evaluate("window.__dup.length")
            pressed = a.get_attribute("#meeting-denoise", "aria-pressed")
            check(dup == 0 and pressed == "true", f"4 переключения подряд: голос А у Б одним потоком (двойных тиков {dup}), шумодав включён ({pressed})")
            n_audio = b.evaluate("document.querySelectorAll('#meeting-stage audio').length")
            check(n_audio == 1, f"у Б на странице один элемент звука на одного собеседника ({n_audio})")

            # --- рекордер голоса выпал из комнаты: людей это не касается, запись продолжается
            time.sleep(2)
            t4 = b.evaluate("performance.now()")
            size_before = track_files(room_name)
            lk_remove(room_name, RECORDER_ID)
            back = wait_until(lambda: RECORDER_ID in {r["identity"] for r in lk_participants(room_name)}, 60, 1.0)
            t5 = b.evaluate("performance.now()")
            check(bool(back), f"рекордер голоса вернулся в комнату ({(t5 - t4) / 1000:.1f} с)")
            kick = route_summary(b.evaluate("window.__route"), t4, t5)
            report["recorder_kick_route"] = kick
            check(kick["none"] == 0 and kick["both"] == 0, f"пока рекордера нет, звук А у Б не меняется ({kick})")
            time.sleep(3)
            size_after = track_files(room_name)
            shrunk = [k for k, v in size_before.items() if size_after.get(k, 0) < v]
            check(not shrunk, f"после переподключения рекордера записи не обрезаны ({len(size_after)} файлов)")
            wav_name = next((k for k in size_after if k.endswith(".wav") and ida.split(":")[-1] in k), "")
            time.sleep(3)
            grew = track_files(room_name).get(wav_name, 0) - size_after.get(wav_name, 0)
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
