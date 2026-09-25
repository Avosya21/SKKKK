const SCREEN_HINT =
  "С телефона шаринг экрана умеют Chrome на Android и Safari на iOS (вкладка, не весь телефон как в Zoom).";
const PUBLISH_DENIED = "нет права публикации. Выйдите из комнаты и войдите снова.";
const IMMERSIVE_CLASS = "is-immersive";
const LAYOUT_GRID = "grid";
const LAYOUT_SPEAKER = "speaker";
const MOBILE_MQ = "(max-width: 575.98px)";
const DOCK_MORE_MQ = "(max-width: 767.98px)";
const ICON_EXPAND =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M7 14H5v5h5v-2H7v-3zm12 5h-5v-2h3v-3h2v5zM7 7h3V5H5v5h2V7zm12 3h-2V7h-3V5h5v5z"/></svg>';
const ICON_COMPRESS =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm8 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z"/></svg>';
const ICON_MIC =
  '<svg class="meeting-chip-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 14a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V21h2v-3.1A7 7 0 0 0 19 11h-2z"/></svg>';
const ICON_MIC_OFF =
  '<svg class="meeting-chip-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M19 11h-1.7a5.3 5.3 0 0 1-.5 2.1l1.3 1.3A7 7 0 0 0 19 11zm-7 3c.4 0 .8-.1 1.2-.2l-1.5-1.5H11.5V6a.5.5 0 0 1 1 0v4.2l4.4 4.4A5 5 0 0 1 7 11H5a7 7 0 0 0 6 6.9V21h2v-3.1c.7-.1 1.3-.4 1.9-.7l-1.4-1.4c-.5.2-1 .3-1.5.3zM4.3 3 3 4.3 9.7 11H9a3 3 0 0 0 3 3c.2 0 .4 0 .6-.1L21.7 22 23 20.7 4.3 3z"/></svg>';
const ICON_CAM =
  '<svg class="meeting-chip-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M17 10.5V7a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-3.5l4 4v-11l-4 4z"/></svg>';
const ICON_CAM_OFF =
  '<svg class="meeting-chip-icon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M3.3 2 2 3.3 4.7 6H4a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h10c.3 0 .5 0 .8-.1L20.7 22 22 20.7 3.3 2zM17 10.5 21 6.5v11l-4-4V15c0 .2 0 .3-.1.5L7.6 8H15a1 1 0 0 1 1 1v1.5z"/></svg>';
const PHONE_UA = /iPhone|iPad|iPod|Android.+Mobile/i;
const DFN3_MODULE = "/static/vendor/dfn3/index.esm.js";
const DFN3_ASSETS = "/static/vendor/dfn3";
const DFN3_CDN = "https://cdn.mezon.ai/AI/models/datas/noise_suppression/deepfilternet3";
const DFN3_SAMPLE_RATE = 48000;
const DFN3_LEVEL = 50;
const DFN3_HINT = "Очистка звука недоступна, обычный микрофон.";
const CLEANER_ID = "skkkk-dfn";
const DENOISE_TOPIC = "skkkk-denoise";
const LOUDNESS_THRESHOLD = -24;
const LOUDNESS_KNEE = 12;
const LOUDNESS_RATIO = 3;
const LOUDNESS_ATTACK = 0.02;
const LOUDNESS_RELEASE = 0.25;
const LOUDNESS_GAIN = 1.1;
const INVITE_KEY = "skkkk-meeting-invite-";
const KIND_AUDIO_OUT = "audiooutput";
const ICE_TIMEOUT_MS = 45000;
const ICE_HINT =
  "Сигналинг есть, медиа не проходит. На роутере нужны TCP 7881, UDP 50000 и UDP 3478.";
const TEXT_SOUND_OFF = "Звук выключен: вы никого не слышите";
const TEXT_MIC_SILENT = "Микрофон молчит. Проверьте устройство";
const TEXT_MIC_RESTART = "Перезапустить";
const TEXT_NO_MIC = "нет микрофона";
const TEXT_NO_CAM = "нет камеры";
const TEXT_MIC_OFF = "Микрофон выключен";
const TEXT_CAM_OFF = "Камера выключена";
const TEXT_MIC_ON = "Микрофон включён";
const TEXT_CAM_ON = "Камера включена";
const TEXT_MIC_SILENT_CHIP = "молчит";
// Индикатор связи в виде палочек (как сотовая сеть): 4 зелёные — отлично, 3 зелёные — хорошо, 1 красная — плохо, 0 — нет связи.
// Ухудшение до «плохо» и возврат из «плохо» подтверждаются задержкой, чтобы палочки не дёргались от кратких скачков.
const SIGNAL_LEVELS = { excellent: 4, good: 3, poor: 1, lost: 0 };
const SIGNAL_LABELS = { 4: "отличное", 3: "хорошее", 1: "плохое", 0: "нет связи" };
const SIGNAL_POOR_DELAY_MS = 2000;
const SIGNAL_RECOVER_DELAY_MS = 3000;
const LINK_TOAST_GAP_MS = 60000;
const LINK_BACK_MIN_SHOWN_MS = 8000;
const TEXT_LINK_POOR_HINT = "Слабая связь. Если голос прерывается, выключите камеру.";
const TEXT_LINK_BETTER = "Связь наладилась.";
const TEXT_SCREEN_PREFIX = "Экран — ";
const TEXT_PWA_HINT = "Добавить на экран";
const TEXT_INVITE_CARD =
  "Ссылка гостя есть во вкладке, где встречу создали, или через «Новая ссылка».";
const PWA_HINT_KEY = "skkkk-pwa-hint";
const TOAST_MS = 4000;
const SCREEN_TILE_PREFIX = "meeting-tile-screen-";
const KIND_AUDIO_IN = "audioinput";
const KIND_VIDEO_IN = "videoinput";
const METER_FFT_SIZE = 2048;
const METER_CENTER = 128;
const METER_HEARD = 0.015;
const METER_GAIN = 4;
const METER_HOLD_MS = 1500;
const VIDEO_FALLBACKS = [
  { width: { ideal: 640 }, height: { ideal: 360 } },
  { width: { max: 1280 }, height: { max: 720 } },
  true,
];

function deviceLabel(device, index, kind) {
  if (device.label) return device.label;
  const prefix =
    kind === KIND_AUDIO_IN ? "Микрофон" : kind === KIND_AUDIO_OUT ? "Динамик" : "Камера";
  return prefix + " " + (index + 1);
}

function canSetSink() {
  return Boolean(HTMLMediaElement.prototype.setSinkId);
}

// Зеркалим своё изображение только с фронтальной камеры. Тыловая (facingMode "environment" или «задняя» в названии) показывается как есть.
function isFrontCamera(mediaTrack) {
  if (!mediaTrack) return true;
  let st = {};
  try {
    st = mediaTrack.getSettings ? mediaTrack.getSettings() : {};
  } catch (_) {
    st = {};
  }
  if (st && st.facingMode) return st.facingMode !== "environment";
  return !/back|rear|environment|задн|тыл/i.test(String(mediaTrack.label || ""));
}

// Ссылку часто открывают во встроенном браузере мессенджера или в старом браузере без WebRTC: вместо английской ошибки библиотеки
// говорим по-русски, что делать.
function noWebRtcText() {
  const ua = String(navigator.userAgent || "");
  const inApp = /Telegram|Instagram|FBAN|FBAV|Line\/|MicroMessenger|VKClient|; wv\)|GSA\//i.test(ua);
  return (
    "В этом браузере звонки не работают (нет WebRTC)." +
    (inApp ? " Похоже, ссылка открыта внутри приложения. " : " ") +
    "Откройте ссылку в Safari (iPhone) или Chrome (Android): нажмите «⋯» → «Открыть в браузере»."
  );
}

const REJOIN_DELAYS_MS = [1000, 2000, 3000, 5000, 8000, 13000, 20000, 30000];
const REJOIN_TOTAL_MS = 15 * 60 * 1000;
const TEXT_LINK_LOST = "Связь пропала, восстанавливаю…";
const TEXT_LINK_BACK = "Связь восстановлена.";
const TEXT_SINK_UNSUPPORTED =
  "Этот браузер не позволяет выбрать динамик со страницы. Переключите звук в настройках телефона или подключите наушники.";

function isMobileLayout() {
  return Boolean(window.matchMedia && window.matchMedia(MOBILE_MQ).matches);
}

function isPhoneLandscape() {
  return Boolean(
    window.matchMedia &&
      window.matchMedia("(max-width: 896px) and (orientation: landscape)").matches,
  );
}

function phoneGridCols(count, landscape) {
  const n = Math.max(1, Number(count) || 1);
  if (n <= 1) return 1;
  if (n <= 2) return landscape ? 2 : 1;
  if (n <= 4) return 2;
  if (n <= 6) return landscape ? 3 : 2;
  if (n <= 9) return 3;
  return 4;
}

function applyPhoneGrid(tilesEl, count) {
  if (!tilesEl) return;
  tilesEl.style.setProperty("--meeting-cols", String(phoneGridCols(count, isPhoneLandscape())));
}

const DESKTOP_GRID_GAP = 12;

// Десктоп: колонки и размер плитки — по ширине И высоте сцены (SKKKKLayout.bestGrid); телефон и режимы
// «фокус/экран сверху» живут на своих правилах CSS.
function applyDesktopGrid(tilesEl, count) {
  if (!tilesEl || !window.SKKKKLayout || !window.SKKKKLayout.bestGrid) return;
  const auto =
    !isPhoneStage() &&
    !tilesEl.classList.contains("has-focus") &&
    !tilesEl.querySelector(".meeting-tile-screen") &&
    count > 1;
  tilesEl.classList.toggle("is-auto-grid", auto);
  if (!auto) {
    tilesEl.style.removeProperty("--meeting-tile-w");
    return;
  }
  const grid = window.SKKKKLayout.bestGrid(count, tilesEl.clientWidth, tilesEl.clientHeight, DESKTOP_GRID_GAP, 16 / 9);
  tilesEl.style.setProperty("--meeting-cols", String(grid.cols));
  tilesEl.style.setProperty("--meeting-tile-w", grid.tileW + "px");
}

function isDockMoreLayout() {
  return Boolean(window.matchMedia && window.matchMedia(DOCK_MORE_MQ).matches);
}

function voiceFlags() {
  const raw = window.SKKKK_MEETING_VOICE || {};
  return { dfn3: raw.dfn3 === true, loudness: raw.loudness !== false };
}

function isPhoneClient() {
  return isMobileLayout() || PHONE_UA.test(navigator.userAgent || "");
}

function isPhoneStage() {
  return isMobileLayout() || isPhoneLandscape();
}

function canUseDfn3() {
  return Boolean(
    voiceFlags().dfn3 &&
      !isPhoneClient() &&
      window.AudioWorklet &&
      window.WebAssembly &&
      window.AudioContext,
  );
}

function isHidden(person) {
  const id = person && person.identity;
  return Boolean(id && String(id).indexOf("skkkk-") === 0);
}

function isCleaner(person) {
  return Boolean(person && person.identity === CLEANER_ID);
}

function hasCleaner(room) {
  if (!room) return false;
  let found = isCleaner(room.localParticipant);
  if (room.remoteParticipants && room.remoteParticipants.forEach) {
    room.remoteParticipants.forEach((person) => {
      if (isCleaner(person)) found = true;
    });
  }
  return found;
}

function parseDenoisePayload(raw) {
  try {
    const data = JSON.parse(raw);
    if (!data || typeof data.denoise !== "boolean") return null;
    return data.denoise;
  } catch (err) {
    return null;
  }
}

function wantsDenoise(person, prefs) {
  const identity = person && person.identity;
  if (prefs && identity && Object.prototype.hasOwnProperty.call(prefs, identity)) {
    return prefs[identity] !== false;
  }
  if (!person) return true;
  const raw = person.metadata;
  if (!raw) return true;
  try {
    return JSON.parse(raw).denoise !== false;
  } catch (err) {
    return true;
  }
}

function trackPublishName(track, pub) {
  return String((pub && (pub.trackName || pub.name)) || (track && track.name) || "");
}

function decodeCleanIdentity(raw) {
  // Заменяем ВСЕ точки на двоеточия
  return String(raw || "").replace(/\./g, ":");
}

function identitiesMatch(left, right) {
  if (!left || !right) return false;
  return left === right || String(left).replace(/:/g, ".") === String(right).replace(/:/g, ".");
}

function cleanSourceId(track, pub) {
  const name = trackPublishName(track, pub);
  return name.indexOf("clean-") === 0 ? decodeCleanIdentity(name.slice(6)) : "";
}

function findPerson(room, identity) {
  if (!room || !identity) return null;
  if (identitiesMatch(room.localParticipant && room.localParticipant.identity, identity)) {
    return room.localParticipant;
  }
  let found = null;
  if (room.remoteParticipants && room.remoteParticipants.forEach) {
    room.remoteParticipants.forEach((person) => {
      if (identitiesMatch(person.identity, identity)) found = person;
    });
  }
  return found;
}

function isOwnIdentity(room, identity) {
  const local = room && room.localParticipant && room.localParticipant.identity;
  return identitiesMatch(local, identity);
}

function playRawAudio(person, room, prefs) {
  if (!person || isCleaner(person)) return false;
  return !hasCleaner(room) || !wantsDenoise(person, prefs);
}

function playCleanTrack(track, room, prefs, pub) {
  const identity = cleanSourceId(track, pub);
  if (!identity || isOwnIdentity(room, identity)) return false;
  return wantsDenoise(findPerson(room, identity), prefs);
}

function canUseLoudness() {
  const Ctor = window.AudioContext;
  return Boolean(
    voiceFlags().loudness &&
      !isPhoneClient() &&
      Ctor &&
      Ctor.prototype &&
      typeof Ctor.prototype.setSinkId === "function",
  );
}

function fillDeviceSelect(select, devices, kind, selectedId) {
  const keep = selectedId || select.value;
  select.replaceChildren();
  devices.forEach((device, index) => {
    const option = document.createElement("option");
    option.value = device.deviceId;
    option.textContent = deviceLabel(device, index, kind);
    select.appendChild(option);
  });
  if (keep && Array.from(select.options).some((item) => item.value === keep)) {
    select.value = keep;
  }
  select.disabled = devices.length === 0;
}

async function listInputDevices() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
    return { audio: [], video: [] };
  }
  try {
    const all = await navigator.mediaDevices.enumerateDevices();
    return {
      audio: all.filter((item) => item.kind === KIND_AUDIO_IN),
      video: all.filter((item) => item.kind === KIND_VIDEO_IN),
    };
  } catch (err) {
    return { audio: [], video: [] };
  }
}

function withDeviceId(base, deviceId) {
  if (!deviceId) return base;
  if (base === true) return { deviceId: { ideal: deviceId } };
  return Object.assign({ deviceId: { ideal: deviceId } }, base);
}

function audioConstraints(deviceId) {
  return withDeviceId(
    { echoCancellation: true, noiseSuppression: false, autoGainControl: true },
    deviceId,
  );
}

function micCaptureOpts(deviceId, dfn3On) {
  const opts = {
    echoCancellation: true,
    autoGainControl: true,
    noiseSuppression: !dfn3On,
  };
  if (deviceId) opts.deviceId = deviceId;
  return opts;
}

async function dfn3AssetConfig() {
  try {
    const probe = await fetch(DFN3_ASSETS + "/v3/pkg/df_bg.wasm", { method: "HEAD" });
    if (probe.ok) return { cdnUrl: DFN3_ASSETS };
  } catch (err) {
    return { cdnUrl: DFN3_CDN };
  }
  return { cdnUrl: DFN3_CDN };
}

function localMicTrack(room) {
  if (!room || !room.localParticipant) return null;
  const LK = window.LivekitClient;
  if (LK && LK.Track && room.localParticipant.getTrackPublication) {
    const pub = room.localParticipant.getTrackPublication(LK.Track.Source.Microphone);
    if (pub && pub.track) return pub.track;
  }
  let found = null;
  if (room.localParticipant.trackPublications) {
    room.localParticipant.trackPublications.forEach((pub) => {
      const source = String(pub.source || "").toLowerCase();
      if (!found && pub.kind === "audio" && pub.track && source.indexOf("screen") < 0) {
        found = pub.track;
      }
    });
  }
  return found;
}

function videoAttempts(deviceId) {
  return VIDEO_FALLBACKS.map((item) => withDeviceId(item, deviceId));
}

function canShareScreen() {
  return Boolean(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
}

function fullscreenElement() {
  return document.fullscreenElement || document.webkitFullscreenElement || null;
}

function nativeFullscreenFn(el) {
  return el && (el.requestFullscreen || el.webkitRequestFullscreen);
}

async function enterNativeFullscreen(el) {
  const req = nativeFullscreenFn(el);
  if (!req) return false;
  try {
    const out = req.call(el);
    if (out && typeof out.then === "function") await out;
    return true;
  } catch (err) {
    return false;
  }
}

async function exitNativeFullscreen() {
  const exit = document.exitFullscreen || document.webkitExitFullscreen;
  if (!exit || !fullscreenElement()) return;
  try {
    const out = exit.call(document);
    if (out && typeof out.then === "function") await out;
  } catch (err) {
    return;
  }
}

function markInlineVideo(node) {
  node.playsInline = true;
  node.setAttribute("playsinline", "");
  node.setAttribute("webkit-playsinline", "");
  node.autoplay = true;
}

function publishErrorText(kind, err) {
  const msg = err && err.message ? String(err.message) : "";
  if (/insufficient permissions/i.test(msg)) return kind + ": " + PUBLISH_DENIED;
  if (err && err.message) return kind + ": " + err.message;
  return kind + " недоступна.";
}

function mediaErrorText(err, kind) {
  const name = err && err.name ? err.name : "";
  if (name === "NotAllowedError") return "Нет разрешения на " + kind + " в браузере.";
  if (name === "NotFoundError") return kind + " не обнаружена. Проверьте подключение устройства.";
  if (name === "NotReadableError") return kind + " занята другим приложением (Zoom, Teams, Телемост).";
  if (err && err.message) return err.message;
  return kind + " недоступна.";
}

async function getMedia(constraints) {
  return navigator.mediaDevices.getUserMedia(constraints);
}

async function openAudioStream(deviceId) {
  return getMedia({ audio: audioConstraints(deviceId), video: false });
}

async function openVideoStream(deviceId, devices) {
  const ids = [];
  if (deviceId) ids.push(deviceId);
  devices.forEach((item) => {
    if (item.deviceId && item.deviceId !== deviceId) ids.push(item.deviceId);
  });
  if (!ids.length) ids.push("");
  let lastErr = null;
  for (let i = 0; i < ids.length; i += 1) {
    const tries = videoAttempts(ids[i]);
    for (let j = 0; j < tries.length; j += 1) {
      try {
        return await getMedia({ audio: false, video: tries[j] });
      } catch (err) {
        lastErr = err;
      }
    }
  }
  throw lastErr || new Error("Камера недоступна.");
}

async function openPreviewTracks(audioId, videoId, videoDevices) {
  const audio = await openAudioStream(audioId);
  let video = null;
  let videoError = null;
  try {
    video = await openVideoStream(videoId, videoDevices || []);
  } catch (err) {
    videoError = err;
  }
  const stream = new MediaStream();
  audio.getAudioTracks().forEach((track) => stream.addTrack(track));
  if (video) video.getVideoTracks().forEach((track) => stream.addTrack(track));
  return { stream: stream, videoError: videoError };
}

function meterRatio(analyser, buffer) {
  analyser.getByteTimeDomainData(buffer);
  let sum = 0;
  for (let i = 0; i < buffer.length; i += 1) {
    const sample = (buffer[i] - METER_CENTER) / METER_CENTER;
    sum += sample * sample;
  }
  return Math.sqrt(sum / buffer.length);
}

const SKKKKMeeting = {
  room: null,
  _joined: false,
  _spotlightId: "",
  _layout: LAYOUT_GRID,
  _pinLocked: false,
  _sinkId: "",
  _endedTimer: 0,
  _heartbeatTimer: 0,
  _dfn3On: false,
  _dfn3: null,
  _serverDenoise: true,
  _soundOn: true,
  _publishedAt: {},
  _quality: {},
  _raf: 0,
  _denoisePrefs: {},
  _playCtx: null,
  _playComp: null,
  _silentMeterCtx: null,
  _silentMeterTimer: 0,
  _silentMeterSource: null,
  _preview: {
    stream: null,
    audioCtx: null,
    meterTimer: 0,
    els: null,
  },

  selectedDevices() {
    const els = this._preview.els || {};
    return {
      audioDeviceId: els.audioSelect && els.audioSelect.value ? els.audioSelect.value : "",
      videoDeviceId: els.videoSelect && els.videoSelect.value ? els.videoSelect.value : "",
    };
  },

  async startPreview(els) {
    this._preview.els = els;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      if (els.statusEl) els.statusEl.textContent = "Браузер не даёт доступ к устройствам.";
      return;
    }
    if (!this._preview.audioCtx || this._preview.audioCtx.state === "closed") {
      this._preview.audioCtx = new AudioContext();
    }
    if (this._preview.audioCtx.state === "suspended") {
      await this._preview.audioCtx.resume();
    }
    try {
      await this._openPreview();
      if (!this._boundDeviceChange) {
        this._boundDeviceChange = () => this._refreshDeviceLists();
        navigator.mediaDevices.addEventListener("devicechange", this._boundDeviceChange);
      }
      if (els.audioSelect) els.audioSelect.onchange = () => this._openPreview();
      if (els.videoSelect) els.videoSelect.onchange = () => this._openPreview();
    } catch (err) {
      if (els.statusEl) {
        els.statusEl.textContent = "Нет доступа к микрофону или камере. Разрешите их в браузере.";
      }
      if (els.micStatusEl) els.micStatusEl.textContent = err && err.message ? err.message : "";
    }
  },

  async stopPreview() {
    if (this._preview.meterTimer) {
      cancelAnimationFrame(this._preview.meterTimer);
      this._preview.meterTimer = 0;
    }
    if (this._meterSource) {
      try { this._meterSource.disconnect(); } catch (_) {}
      this._meterSource = null;
    }
    if (this._preview.stream) {
      this._preview.stream.getTracks().forEach((track) => track.stop());
      this._preview.stream = null;
    }
    if (this._preview.audioCtx) {
      await this._preview.audioCtx.close().catch(() => undefined);
      this._preview.audioCtx = null;
    }
    const els = this._preview.els;
    if (els && els.videoEl) els.videoEl.srcObject = null;
  },

  async connect(livekit, els, devices) {
    const statusEl = els.statusEl;
    this._joined = false;
    if (!livekit || !livekit.configured || !livekit.url || !livekit.token) {
      statusEl.textContent = "Комната ещё не настроена. Голос появится после запуска LiveKit.";
      return null;
    }
    const LK = window.LivekitClient;
    if (!LK) {
      statusEl.textContent = "Не загрузился клиент комнаты.";
      return null;
    }
    this._leaving = false;
    this._rejoining = false;
    this._lastCred = livekit;
    if (typeof LK.isBrowserSupported === "function" && !LK.isBrowserSupported()) {
      statusEl.textContent = noWebRtcText();
      for (const btn of [els.micBtn, els.camBtn, els.screenBtn, els.soundBtn, els.camSwitchBtn]) {
        if (btn) btn.disabled = true;
      }
      return null;
    }
    // Библиотека пишет в консоль как error безобидное «track for a participant that's not present» (серверный бот
    // публикует трек раньше события входа). Оставляем лог только по флагу localStorage.skkkk_lk_debug=1.
    try {
      if (LK.setLogLevel && LK.LogLevel && localStorage.getItem("skkkk_lk_debug") !== "1") {
        LK.setLogLevel(LK.LogLevel.silent);
      }
    } catch (_) {}
    const room = new LK.Room({
      adaptiveStream: true,
      dynacast: true,
      disconnectOnPageLeave: true,
      videoCaptureDefaults: {
        resolution: isPhoneClient() ? LK.VideoPresets.h360 : LK.VideoPresets.h540,
      },
      publishDefaults: {
        simulcast: true,
        videoSimulcastLayers: [LK.VideoPresets.h180, LK.VideoPresets.h360],
        dtx: true,
        red: true,
        screenShareEncoding: LK.ScreenSharePresets.h720fps5.encoding,
      },
      audioCaptureDefaults: {
        echoCancellation: true,
        autoGainControl: true,
        noiseSuppression: !canUseDfn3(),
      },
    });
    this.room = room;
    this._bind(room, els);
    this._bindChrome(els);
    if (els.screenBtn) {
      if (isPhoneClient()) {
        els.screenBtn.hidden = true;
      } else if (!canShareScreen()) {
        els.screenBtn.disabled = true;
        els.screenBtn.title = SCREEN_HINT;
      }
    }
    statusEl.textContent = "Подключаюсь к комнате…";

    let iceTimer = null;
    try {
      const connectPromise = room.connect(livekit.url, livekit.token, { peerConnectionTimeout: ICE_TIMEOUT_MS });
      const timeoutPromise = new Promise((_, reject) => {
        iceTimer = setTimeout(() => reject(new Error(ICE_HINT)), ICE_TIMEOUT_MS);
      });
      await Promise.race([connectPromise, timeoutPromise]);
      if (iceTimer) clearTimeout(iceTimer);

      this._joined = true;
      this._startHeartbeat(els);
      if (!this._deviceListener && navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
        this._deviceListener = () => this._onDeviceChange();
        navigator.mediaDevices.addEventListener("devicechange", this._deviceListener);
        window.addEventListener("online", () => {
          if (this._rejoinWake) this._rejoinWake();
        });
        // Телефон: экран не должен гаснуть во время звонка, а звук после возврата из фона оживает
        document.addEventListener("visibilitychange", () => {
          if (document.visibilityState !== "visible" || this._leaving) return;
          this._requestWakeLock();
          this._ensureAudioContexts();
          if (this.room && this.room.startAudio) Promise.resolve(this.room.startAudio()).catch(() => undefined);
          if (this._rejoinWake) this._rejoinWake();
        });
      }
      this._requestWakeLock();
      this._serverDenoise = true;
      this._soundOn = true;
      this._denoisePrefs = {};
      this._dfn3On = false;

      // Безопасное подключение микрофона (не выбрасывает из комнаты при ошибке разрешений)
      const audioOpts = micCaptureOpts(devices && devices.audioDeviceId, this._dfn3On);
      try {
        await room.localParticipant.setMicrophoneEnabled(true, audioOpts);
        this._watchLocalMic();
        if (this._dfn3On && !(await this._startDfn3())) {
          this._dfn3On = false;
          await this._restartMic(false);
          this._toast(DFN3_HINT);
        }
      } catch (micErr) {
        this._toast(mediaErrorText(micErr, "Микрофон"));
      }

      // Безопасное подключение камеры
      const videoOpts = devices && devices.videoDeviceId ? { deviceId: devices.videoDeviceId } : undefined;
      try {
        await room.localParticipant.setCameraEnabled(Boolean(videoOpts), videoOpts);
      } catch (camErr) {
        this._toast(mediaErrorText(camErr, "Камера"));
      }

      statusEl.textContent = "Вы в комнате.";
      this._renderTiles(room, els);
      this._participants(room).forEach((person) => {
        if (person === room.localParticipant) return;
        person.trackPublications.forEach((pub) => {
          if (pub.track && pub.kind === "audio") this._attachTrack(els, person, pub.track, pub);
        });
      });
      this._bindPhoneGrid();
      this._syncControls(els);
      this._ensureSignal();
      this.refreshSpeakers();
      this.refreshMics();
      this.watchEnded(els);
      this._maybePwaHint();
    } catch (err) {
      if (iceTimer) clearTimeout(iceTimer);
      try { await room.disconnect(); } catch (_) {}
      if (this.room === room) this.room = null;
      this._joined = false;
      const raw = err && err.message ? String(err.message) : String(err);
      statusEl.textContent = /seem to be supported|webrtc/i.test(raw)
        ? noWebRtcText()
        : "Не удалось войти в комнату: " + raw;
    }
    return room;
  },

  async toggleMic(btn) {
    if (!this.room) return;
    try {
      const next = !this.room.localParticipant.isMicrophoneEnabled;
      const picked = this.selectedDevices();
      await this.room.localParticipant.setMicrophoneEnabled(
        next,
        next ? micCaptureOpts(this._pickedMicId || picked.audioDeviceId, this._dfn3On) : undefined,
      );
      if (next) {
        await this._ensureAudioContexts();
        if (this._dfn3On && !(await this._startDfn3())) {
          this._dfn3On = false;
          await this._restartMic(false);
          this._toast(DFN3_HINT);
        }
        this._watchLocalMic();
      } else {
        this._stopMicLevelWatch();
        this._clearMicSilent();
      }
    } catch (err) {
      this._setStatus("Микрофон: " + (err && err.message ? err.message : err));
    } finally {
      this._syncControls(this._roomEls);
      this._renderTiles(this.room, this._roomEls);
    }
  },

  async toggleCam(btn) {
    if (!this.room) return;
    try {
      const next = !this.room.localParticipant.isCameraEnabled;
      const picked = this.selectedDevices();
      const opts = picked.videoDeviceId ? { deviceId: picked.videoDeviceId } : undefined;
      await this.room.localParticipant.setCameraEnabled(next, opts);
    } catch (err) {
      this._setStatus(publishErrorText("Камера", err));
    }
    this._syncControls(this._roomEls);
    this._renderTiles(this.room, this._roomEls);
  },

  async switchCamera() {
    if (!this.room) return;
    const local = this.room.localParticipant;
    if (!local.isCameraEnabled) return;
    try {
      const cams = (await listInputDevices()).video.filter((item) => item.deviceId);
      if (cams.length < 2) {
        this._toast("Другая камера не найдена");
        return;
      }
      const pub = local.getTrackPublication && local.getTrackPublication("camera");
      const track = pub && pub.track && pub.track.mediaStreamTrack;
      const current = track && track.getSettings ? track.getSettings().deviceId : "";
      const idx = cams.findIndex((item) => item.deviceId === current);
      const next = cams[(idx + 1) % cams.length];
      await this.room.switchActiveDevice(KIND_VIDEO_IN, next.deviceId);
      this._syncLocalMirror();
      this._toast("Камера: " + (next.label || "следующая"));
    } catch (err) {
      this._setStatus(publishErrorText("Камера", err));
    }
    this._syncControls(this._roomEls);
    this._renderTiles(this.room, this._roomEls);
  },

  async _refreshCamSwitch(retry = true) {
    const btn = this._roomEls && this._roomEls.camSwitchBtn;
    if (!btn) return;
    const on = Boolean(this.room && this.room.localParticipant.isCameraEnabled);
    let many = false;
    if (on) many = (await listInputDevices()).video.length > 1;
    btn.hidden = !(on && many);
    // Chrome на Android сразу после включения камеры может отдать неполный список устройств: спросим ещё раз
    if (on && !many && retry) {
      setTimeout(() => {
        if (this.room) this._refreshCamSwitch(false);
      }, 1500);
    }
  },

  async toggleScreen(btn) {
    if (!this.room) return;
    const on = this.room.localParticipant.isScreenShareEnabled;
    if (!on && !canShareScreen()) {
      this._setStatus(SCREEN_HINT);
      this._syncControls(this._roomEls);
      return;
    }
    try {
      await this.room.localParticipant.setScreenShareEnabled(!on, { contentHint: "detail" });
    } catch (err) {
      const msg = err && err.message ? String(err.message) : "";
      this._setStatus(
        /not supported|getDisplayMedia|permission|denied/i.test(msg)
          ? SCREEN_HINT
          : publishErrorText("Экран", err),
      );
    }
    this._syncControls(this._roomEls);
    this._renderTiles(this.room, this._roomEls);
  },

  async leaveRoom() {
    this._leaving = true;
    if (this._rejoinWake) this._rejoinWake();
    this._releaseWakeLock();
    this._stopAllTimers();
    await this._stopVoice();
    const leaveApiUrl = this._roomEls && this._roomEls.leaveApiUrl;
    const identity = this._roomEls && this._roomEls.identity;
    if (leaveApiUrl && identity) {
      try {
        const body = JSON.stringify({ identity });
        if (window.SKKKK && SKKKK.apiJson) await SKKKK.apiJson(leaveApiUrl, { method: "POST", body });
        else await fetch(leaveApiUrl, { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true });
      } catch (_) {}
    }
    if (this.room) {
      try { await this.room.disconnect(); } catch (_) {}
      this.room = null;
    }
    const url = this._roomEls && this._roomEls.leaveUrl;
    window.location.href = url || window.location.href;
  },

  _showDuplicateSession(els) {
    // Вы вошли в эту же комнату с другой вкладки/устройства: эту сессию LiveKit закрыл. Не переподключаемся сами
    // (иначе две вкладки будут бесконечно выбивать друг друга) — даём явную кнопку.
    for (const btn of [els.micBtn, els.camBtn, els.screenBtn, els.soundBtn, els.camSwitchBtn]) {
      if (btn) btn.disabled = true;
    }
    if (!els.statusEl) return;
    els.statusEl.textContent = "Вы вошли в эту комнату с другой вкладки. Здесь связь закрыта. ";
    const back = document.createElement("button");
    back.type = "button";
    back.className = "btn btn-sm btn-light ms-2";
    back.id = "meeting-duplicate-return";
    back.textContent = "Вернуться сюда";
    back.addEventListener("click", () => window.location.reload());
    els.statusEl.appendChild(back);
  },

  _stopAllTimers() {
    if (this._heartbeatTimer) {
      clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = 0;
    }
    if (this._endedTimer) {
      clearInterval(this._endedTimer);
      this._endedTimer = 0;
    }
    this._stopMicLevelWatch();
  },

  _startHeartbeat(els) {
    if (this._heartbeatTimer || !els || !els.heartbeatUrl || !els.identity) return;
    const beat = () => {
      if (!this._joined) return;
      const body = JSON.stringify({ identity: els.identity });
      if (els.authedApi && window.SKKKK && SKKKK.apiJson) {
        Promise.resolve(SKKKK.apiJson(els.heartbeatUrl, { method: "POST", body })).catch(() => undefined);
        return;
      }
      fetch(els.heartbeatUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
        keepalive: true,
      }).catch(() => undefined);
    };
    beat();
    this._heartbeatTimer = setInterval(beat, 20000);
  },

  // Своё окно подтверждения вместо window.confirm: единый стиль, не блокирует медиа и не пропускается автотестами.
  _confirm(message, okLabel) {
    return new Promise((resolve) => {
      const wrap = document.createElement("div");
      wrap.className = "meeting-confirm";
      wrap.setAttribute("role", "alertdialog");
      wrap.setAttribute("aria-modal", "true");
      wrap.innerHTML =
        '<div class="meeting-confirm-box"><p class="meeting-confirm-text"></p>' +
        '<div class="meeting-confirm-actions"><button type="button" class="btn btn-secondary" data-act="no">Отмена</button>' +
        '<button type="button" class="btn btn-danger" data-act="yes"></button></div></div>';
      wrap.querySelector(".meeting-confirm-text").textContent = message;
      wrap.querySelector('[data-act="yes"]').textContent = okLabel || "Да";
      const done = (value) => {
        document.removeEventListener("keydown", onKey);
        wrap.remove();
        resolve(value);
      };
      const onKey = (ev) => {
        if (ev.key === "Escape") done(false);
      };
      document.addEventListener("keydown", onKey);
      wrap.addEventListener("click", (ev) => {
        const act = ev.target && ev.target.getAttribute && ev.target.getAttribute("data-act");
        if (act === "yes") done(true);
        else if (act === "no" || ev.target === wrap) done(false);
      });
      (this._roomEls && this._roomEls.stageEl ? this._roomEls.stageEl : document.body).appendChild(wrap);
      wrap.querySelector('[data-act="no"]').focus();
    });
  },

  async endForAll() {
    const url = this._roomEls && this._roomEls.endUrl;
    if (!url) return;
    if (!(await this._confirm("Завершить встречу для всех участников?", "Завершить для всех"))) return;
    try {
      if (window.SKKKK && SKKKK.apiJson) await SKKKK.apiJson(url, { method: "POST" });
      else await fetch(url, { method: "POST" });
    } catch (err) {
      this._setStatus("Не удалось завершить встречу: " + (err && err.message ? err.message : err));
      return;
    }
    await this.leaveRoom();
  },

  watchEnded(els) {
    const token = els && els.publicToken;
    const endUrl = els && (els.watchEndedUrl || (token ? "/api/meetings/public/" + encodeURIComponent(token) : null));
    if (!endUrl || this._endedTimer) return;
    this._endedTimer = setInterval(async () => {
      try {
        const res = await fetch(endUrl);
        if (res.status === 404 || res.status === 410) {
          // Встречу удалили или ссылку отозвали: без этого клиент бесконечно переподключается по старому токену
          // LiveKit, комната создаётся заново, и запись возобновляется в новом каталоге (находка аудита 4.9).
          this._setStatus("Встреча недоступна: она удалена или ссылка отозвана.");
          await this.leaveRoom();
          return;
        }
        if (!res.ok) return;
        const data = await res.json();
        if (data.ended || data.status === "ended") {
          this._setStatus("Встреча завершена.");
          await this.leaveRoom();
        }
      } catch (err) {
        return;
      }
    }, 4000);
  },

  inviteUrl() {
    const els = this._roomEls || {};
    if (els.inviteUrl) return els.inviteUrl;
    if (els.meetingId) {
      const saved = sessionStorage.getItem(INVITE_KEY + els.meetingId);
      if (saved) return saved;
    }
    if (window.location.pathname.indexOf("/m/") === 0) return window.location.href;
    return "";
  },

  async copyInvite() {
    const url = this.inviteUrl();
    if (!url) {
      this._toast(TEXT_INVITE_CARD);
      return;
    }
    try {
      await navigator.clipboard.writeText(url);
      this._toast("Ссылка скопирована");
    } catch (err) {
      this._toast("Не удалось скопировать ссылку");
    }
  },

  toggleLayout() {
    if (this._layout === LAYOUT_GRID) {
      this._layout = LAYOUT_SPEAKER;
      this._pinLocked = false;
      const tile = this._screenTile() || this._speakingTile() || this._firstTile();
      this._setSpotlight(tile);
    } else {
      this._layout = LAYOUT_GRID;
      this._pinLocked = false;
      if (!this.isImmersive()) this._clearSpotlight();
    }
    this._syncLayoutBtn();
  },

  toggleRoster(force) {
    const panel = this._roomEls && this._roomEls.rosterPanel;
    const btn = this._roomEls && this._roomEls.rosterBtn;
    const back = this._roomEls && this._roomEls.rosterBackdrop;
    if (!panel) return;
    const next = typeof force === "boolean" ? force : panel.hidden;
    panel.hidden = !next;
    if (back) back.hidden = !next;
    if (btn) btn.setAttribute("aria-pressed", next ? "true" : "false");
    if (next) {
      this.toggleMore(false);
      this.toggleSpeakerMenu(false);
    }
  },

  toggleMore(force) {
    const tray = this._roomEls && this._roomEls.moreTray;
    const btn = this._roomEls && this._roomEls.moreBtn;
    if (!tray) return;
    if (!isDockMoreLayout()) {
      tray.classList.remove("is-open");
      if (btn) btn.setAttribute("aria-expanded", "false");
      return;
    }
    const open = typeof force === "boolean" ? force : !tray.classList.contains("is-open");
    tray.classList.toggle("is-open", open);
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      this.toggleRoster(false);
      this.toggleSpeakerMenu(false);
    }
  },

  focusParticipant(tile) {
    if (!tile) return;
    this._layout = LAYOUT_SPEAKER;
    this._pinLocked = true;
    this._setSpotlight(tile);
    this._syncLayoutBtn();
  },

  async refreshSpeakers() {
    const wrap = this._roomEls && this._roomEls.speakerWrap;
    const select = this._roomEls && this._roomEls.speakerSelect;
    if (!wrap || !select) return;
    if (!canSetSink()) {
      wrap.hidden = false;
      const menu = this._roomEls && this._roomEls.speakerMenu;
      if (menu) {
        menu.replaceChildren();
        const info = document.createElement("div");
        info.className = "meeting-speaker-item";
        info.setAttribute("role", "note");
        info.textContent = TEXT_SINK_UNSUPPORTED;
        menu.appendChild(info);
      }
      return;
    }
    wrap.hidden = false;
    const all = await navigator.mediaDevices.enumerateDevices();
    const outs = all.filter((item) => item.kind === KIND_AUDIO_OUT);
    fillDeviceSelect(select, outs, KIND_AUDIO_OUT, this._sinkId);
    this._paintSpeakerMenu(select);
    if (!select.onchange) {
      select.onchange = () => this.setSink(select.value);
    }
  },

  // Выбор микрофона прямо в комнате (по аналогии с камерой и динамиком). Кнопка создаётся здесь, разметка страниц не меняется.
  _ensureMicPicker() {
    const els = this._roomEls;
    if (!els || els.micPickWrap) return els && els.micPickWrap;
    const anchor = els.speakerWrap || els.moreTray;
    if (!anchor || !anchor.parentNode) return null;
    const wrap = document.createElement("div");
    wrap.className = "meeting-dock-speaker";
    wrap.id = "meeting-mic-pick-wrap";
    wrap.hidden = true;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "meeting-dock-btn";
    btn.id = "meeting-mic-pick-btn";
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-label", "Выбрать микрофон");
    btn.title = "Выбрать микрофон";
    btn.innerHTML =
      '<svg viewBox="0 0 24 24" aria-hidden="true"><defs><mask id="mic-cfg-cut"><rect width="24" height="24" fill="#fff"/><circle cx="17.6" cy="17.4" r="6.9" fill="#000"/></mask></defs><g mask="url(#mic-cfg-cut)"><rect x="7" y="1.6" width="8" height="14" rx="4" fill="currentColor"/><path d="M4.6 11.4V13a6.1 6.1 0 0 0 6.1 6.1v2.3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></g><path fill="currentColor" fill-rule="evenodd" stroke="currentColor" stroke-width=".6" stroke-linejoin="round" d="M16.56 13.23 L16.71 11.77 L18.49 11.77 L18.64 13.23 L20.69 14.41 L22.03 13.81 L22.92 15.36 L21.73 16.21 L21.73 18.59 L22.92 19.44 L22.03 20.99 L20.69 20.39 L18.64 21.57 L18.49 23.03 L16.71 23.03 L16.56 21.57 L14.51 20.39 L13.17 20.99 L12.28 19.44 L13.47 18.59 L13.47 16.21 L12.28 15.36 L13.17 13.81 L14.51 14.41ZM15.900000000000002 17.4a1.7 1.7 0 1 0 3.4 0a1.7 1.7 0 1 0 -3.4 0Z"/></svg>' +
      '<span class="visually-hidden">Выбрать микрофон</span>';
    const menu = document.createElement("div");
    menu.id = "meeting-mic-pick-menu";
    menu.className = "meeting-speaker-menu";
    menu.hidden = true;
    menu.setAttribute("role", "listbox");
    menu.setAttribute("aria-label", "Микрофон");
    wrap.appendChild(btn);
    wrap.appendChild(menu);
    anchor.parentNode.insertBefore(wrap, els.speakerWrap || anchor);
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        this.toggleSpeakerMenu(false);
        this.toggleRoster(false);
      }
    });
    menu.addEventListener("click", (event) => event.stopPropagation());
    document.addEventListener("click", () => {
      menu.hidden = true;
      btn.setAttribute("aria-expanded", "false");
    });
    els.micPickWrap = wrap;
    els.micPickBtn = btn;
    els.micPickMenu = menu;
    return wrap;
  },

  _currentMicId() {
    const pub = this.room && this.room.localParticipant && this.room.localParticipant.getTrackPublication("microphone");
    const track = pub && pub.track && pub.track.mediaStreamTrack;
    try {
      return (track && track.getSettings && track.getSettings().deviceId) || "";
    } catch (_) {
      return "";
    }
  },

  async refreshMics() {
    const wrap = this._ensureMicPicker();
    const els = this._roomEls;
    if (!wrap || !els || !els.micPickMenu) return;
    const mics = (await listInputDevices()).audio.filter((item) => item.deviceId && item.deviceId !== "communications");
    wrap.hidden = mics.length < 2;
    const current = this._pickedMicId || this._currentMicId();
    els.micPickMenu.replaceChildren();
    mics.forEach((device, index) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "meeting-speaker-item";
      item.textContent = deviceLabel(device, index, KIND_AUDIO_IN);
      if (device.deviceId === current) item.setAttribute("aria-current", "true");
      item.addEventListener("click", () => {
        els.micPickMenu.hidden = true;
        this.setMic(device.deviceId, item.textContent);
      });
      els.micPickMenu.appendChild(item);
    });
  },

  // Смена микрофона в комнате. Выбор запоминается: включение/выключение микрофона дальше идёт с этим устройством.
  async setMic(deviceId, label) {
    if (!this.room || !deviceId) return;
    this._pickedMicId = deviceId;
    const lobby = (this._preview && this._preview.els) || {};
    if (lobby.audioSelect && Array.from(lobby.audioSelect.options).some((o) => o.value === deviceId)) {
      lobby.audioSelect.value = deviceId;
    }
    const local = this.room.localParticipant;
    if (!local.isMicrophoneEnabled) {
      this._toast("Микрофон: " + (label || "выбран") + ". Он включится при включении звука.");
      this.refreshMics();
      return;
    }
    try {
      if (this._dfn3On) {
        await this._restartMic(true);
      } else {
        await this.room.switchActiveDevice(KIND_AUDIO_IN, deviceId);
        this._watchLocalMic();
      }
      this._toast("Микрофон: " + (label || "выбран"));
    } catch (err) {
      this._toast(mediaErrorText(err, "Микрофон"));
    }
    this.refreshMics();
  },

  // Устройства подключили или отключили: обновить списки, а если пропал используемый микрофон, перейти на доступный.
  async _onDeviceChange() {
    if (!this.room) return;
    this.refreshSpeakers();
    this._refreshCamSwitch();
    await this.refreshMics();
    const local = this.room.localParticipant;
    if (!local.isMicrophoneEnabled) return;
    const mics = (await listInputDevices()).audio.filter((item) => item.deviceId);
    const current = this._currentMicId();
    const alive = this.room.localParticipant.getTrackPublication("microphone");
    const ended = alive && alive.track && alive.track.mediaStreamTrack && alive.track.mediaStreamTrack.readyState === "ended";
    if (mics.length && (ended || (current && !mics.some((item) => item.deviceId === current)))) {
      try {
        await this.room.switchActiveDevice(KIND_AUDIO_IN, mics[0].deviceId);
        this._pickedMicId = mics[0].deviceId;
        this._toast("Микрофон отключён, переключился на «" + (mics[0].label || "другой") + "»");
        this.refreshMics();
      } catch (err) {
        this._toast(mediaErrorText(err, "Микрофон"));
      }
    }
  },

  async _requestWakeLock() {
    if (this._leaving || this._wakeLock || !navigator.wakeLock || !navigator.wakeLock.request) return;
    try {
      const lock = await navigator.wakeLock.request("screen");
      this._wakeLock = lock;
      lock.addEventListener("release", () => {
        if (this._wakeLock === lock) this._wakeLock = null;
      });
    } catch (_) {
      this._wakeLock = null;
    }
  },

  _releaseWakeLock() {
    const lock = this._wakeLock;
    this._wakeLock = null;
    if (lock && lock.release) lock.release().catch(() => undefined);
  },

  // Индикатор своей связи: четыре палочки вверху комнаты (строку статуса не трогаем). Виден всегда, по наведению подсказка словами.
  _ensureSignal() {
    let box = document.getElementById("meeting-signal");
    if (box) return box;
    const top = this._roomEls && this._roomEls.stageEl && this._roomEls.stageEl.querySelector(".meeting-stage-top");
    if (!top) return null;
    box = document.createElement("div");
    box.id = "meeting-signal";
    box.className = "meeting-signal";
    box.setAttribute("role", "img");
    box.dataset.level = "unknown";
    box.setAttribute("aria-label", "Качество связи: проверяется");
    box.title = "Качество связи: проверяется";
    for (let n = 0; n < 4; n += 1) box.appendChild(document.createElement("i"));
    top.appendChild(box);
    return box;
  },

  _paintSignal(level) {
    const box = this._ensureSignal();
    if (!box) return;
    const text = level === "unknown" ? "проверяется" : SIGNAL_LABELS[level];
    box.dataset.level = String(level);
    box.setAttribute("aria-label", "Качество связи: " + text);
    box.title = "Качество связи: " + text;
  },

  _linkQuality(quality) {
    const key = String(quality && typeof quality === "object" ? quality.quality || quality.value || "" : quality || "").toLowerCase();
    if (!(key in SIGNAL_LEVELS)) return;
    const target = SIGNAL_LEVELS[key];
    const current = this._sigLevel === undefined ? "unknown" : this._sigLevel;
    if (target === current) {
      clearTimeout(this._sigTimer);
      this._sigTimer = null;
      this._sigPending = null;
      return;
    }
    // «Плохо» подтверждается 2 с, возврат из плохого 3 с; обрыв и колебания хорошо/отлично показываются сразу
    let delay = 0;
    if (target === 1) delay = SIGNAL_POOR_DELAY_MS;
    else if (typeof current === "number" && current <= 1 && target >= 3) delay = SIGNAL_RECOVER_DELAY_MS;
    if (delay === 0) {
      this._applySignal(target);
      return;
    }
    if (this._sigPending === target) return;
    clearTimeout(this._sigTimer);
    this._sigPending = target;
    this._sigTimer = setTimeout(() => {
      this._sigTimer = null;
      this._sigPending = null;
      this._applySignal(target);
    }, delay);
  },

  _applySignal(level) {
    clearTimeout(this._sigTimer);
    this._sigTimer = null;
    this._sigPending = null;
    const before = this._sigLevel;
    this._sigLevel = level;
    this._paintSignal(level);
    if (this.room && this._roomEls) this._renderTiles(this.room, this._roomEls);
    const now = Date.now();
    if (level === 1) {
      this._sigBadAt = this._sigBadAt || now;
      if (now - (this._qToastAt || 0) > LINK_TOAST_GAP_MS) {
        this._qToastAt = now;
        const camOn = Boolean(this.room && this.room.localParticipant && this.room.localParticipant.isCameraEnabled);
        this._toast(TEXT_LINK_POOR_HINT, camOn ? { label: "Выключить камеру", run: () => this.toggleCam() } : undefined);
      }
    } else if (level >= 3) {
      const badFor = this._sigBadAt ? now - this._sigBadAt : 0;
      this._sigBadAt = 0;
      if (typeof before === "number" && before <= 1 && badFor >= LINK_BACK_MIN_SHOWN_MS) this._toast(TEXT_LINK_BETTER);
    }
  },

  _resetLinkQuality(level) {
    clearTimeout(this._sigTimer);
    this._sigTimer = null;
    this._sigPending = null;
    this._sigBadAt = 0;
    this._sigLevel = level === undefined ? undefined : level;
    if (document.getElementById("meeting-signal")) this._paintSignal(level === undefined ? "unknown" : level);
  },

  _netStatus(text, bad) {
    const el = this._roomEls && this._roomEls.statusEl;
    if (el) el.textContent = text;
    if (bad) this._toast(text);
  },

  // Связь оборвалась насовсем (библиотека уже пробовала восстановить сама): возвращаемся в комнату с тем же именем участника,
  // пока встреча идёт. Повторный вход с тем же identity вытесняет «зависшую» старую сессию, место в комнате сохраняется.
  _beginRejoin(els) {
    if (this._rejoining || this._leaving) return;
    this._rejoining = true;
    this._rejoinLoop(els).finally(() => {
      this._rejoining = false;
    });
  },

  _rejoinWait(ms) {
    return new Promise((resolve) => {
      const t = setTimeout(resolve, ms);
      this._rejoinWake = () => {
        clearTimeout(t);
        resolve();
      };
    });
  },

  async _rejoinLoop(els) {
    const LK = window.LivekitClient;
    const media = this._lastMedia || { mic: true, cam: false };
    const started = Date.now();
    this._netStatus(TEXT_LINK_LOST, false);
    for (let attempt = 0; !this._leaving && Date.now() - started < REJOIN_TOTAL_MS; attempt += 1) {
      await this._rejoinWait(REJOIN_DELAYS_MS[Math.min(attempt, REJOIN_DELAYS_MS.length - 1)]);
      if (this._leaving || !this.room) return;
      if (navigator.onLine === false) {
        this._netStatus("Нет сети. Как только появится, вернусь в комнату…", false);
        continue;
      }
      this._netStatus("Возвращаюсь в комнату… (попытка " + (attempt + 1) + ")", false);
      try {
        let cred = this._lastCred;
        if (typeof els.refreshJoin === "function") {
          const data = await els.refreshJoin();
          if (data && data.livekit && data.livekit.token) {
            cred = data.livekit;
            if (data.identity) els.identity = data.identity;
          }
        }
        if (!cred || !cred.token) continue;
        this._lastCred = cred;
        await this.room.connect(cred.url, cred.token, { peerConnectionTimeout: ICE_TIMEOUT_MS });
      } catch (err) {
        if (err && err.meetingGone) {
          this._netStatus(err.message || "Встреча завершена или недоступна.", false);
          return;
        }
        continue;
      }
      this._joined = true;
      this._startHeartbeat(els);
      this.watchEnded(els);
      try {
        if (media.mic) await this.room.localParticipant.setMicrophoneEnabled(true, micCaptureOpts(this._pickedMicId || this.selectedDevices().audioDeviceId, this._dfn3On));
        if (media.cam) await this.room.localParticipant.setCameraEnabled(true, this.selectedDevices().videoDeviceId ? { deviceId: this.selectedDevices().videoDeviceId } : undefined);
        if (media.mic) this._watchLocalMic();
      } catch (mediaErr) {
        this._toast(mediaErrorText(mediaErr, "Микрофон"));
      }
      this._renderTiles(this.room, els);
      this._reattachAudio();
      this._syncControls(els);
      this._netStatus(TEXT_LINK_BACK, false);
      this._toast(TEXT_LINK_BACK);
      return;
    }
    if (this._leaving) return;
    if (els.statusEl) {
      els.statusEl.textContent = "Не удалось восстановить связь. ";
      const back = document.createElement("button");
      back.type = "button";
      back.className = "btn btn-sm btn-light ms-2";
      back.id = "meeting-rejoin-reload";
      back.textContent = "Войти снова";
      back.addEventListener("click", () => window.location.reload());
      els.statusEl.appendChild(back);
    }
    void LK;
  },

  toggleSpeakerMenu(force) {
    const menu = this._roomEls && this._roomEls.speakerMenu;
    const btn = this._roomEls && this._roomEls.speakerBtn;
    if (!menu) return;
    const open = typeof force === "boolean" ? force : menu.hidden;
    menu.hidden = !open;
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) this.toggleRoster(false);
  },

  _paintSpeakerMenu(select) {
    const menu = this._roomEls && this._roomEls.speakerMenu;
    if (!menu) return;
    menu.replaceChildren();
    Array.from(select.options).forEach((opt) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "meeting-speaker-item";
      item.textContent = opt.textContent;
      if (opt.selected) item.setAttribute("aria-current", "true");
      item.addEventListener("click", () => {
        select.value = opt.value;
        this.setSink(opt.value);
        this.toggleSpeakerMenu(false);
        this._paintSpeakerMenu(select);
      });
      menu.appendChild(item);
    });
  },

  toggleSound() {
    this._soundOn = !this._soundOn;
    const pubs = this._remoteAudioPubs();
    window.SKKKKLayout.soundPlan(this._soundOn, pubs).forEach((row) => {
      if (row.pub && row.pub.setSubscribed) row.pub.setSubscribed(row.subscribed);
    });
    if (!this._soundOn) {
      this._toast(TEXT_SOUND_OFF);
    } else {
      // КРИТИЧНО: При включении звука гарантированно прикрепляем аудиофайлы обратно в DOM
      this._reattachAudio();
    }
    this._syncControls(this._roomEls);
  },

  _remoteAudioPubs() {
    const out = [];
    if (!this.room) return out;
    this.room.remoteParticipants.forEach((person) => {
      person.trackPublications.forEach((pub) => {
        if (pub.kind === "audio") out.push(pub);
      });
    });
    return out;
  },

  async toggleDenoise() {
    const next = !this._serverDenoise;
    const local = this.room && this.room.localParticipant;
    this._serverDenoise = next;
    if (local && local.identity) this._denoisePrefs[local.identity] = next;
    this._syncControls(this._roomEls);
    this._reattachAudio();
    if (!(await this._publishDenoise(next))) {
      this._serverDenoise = !next;
      if (local && local.identity) this._denoisePrefs[local.identity] = !next;
      this._syncControls(this._roomEls);
      this._reattachAudio();
      this._toast("Не удалось сменить шумодав");
      return;
    }
    this._setDenoiseMeta(next);
  },

  async _publishDenoise(on) {
    const local = this.room && this.room.localParticipant;
    if (!local || typeof local.publishData !== "function") return false;
    try {
      const body = new TextEncoder().encode(JSON.stringify({ denoise: on }));
      await local.publishData(body, { reliable: true, topic: DENOISE_TOPIC });
      return true;
    } catch (err) {
      return false;
    }
  },

  _setDenoiseMeta(on) {
    const local = this.room && this.room.localParticipant;
    if (!local || typeof local.setMetadata !== "function") return;
    try {
      const pending = local.setMetadata(JSON.stringify({ denoise: on }));
      if (pending && typeof pending.catch === "function") pending.catch(() => undefined);
    } catch (err) {
      return;
    }
  },

  async setSink(deviceId) {
    this._sinkId = deviceId || "";
    let errorToastShown = false;
    if (this._playCtx && this._playCtx.setSinkId && this._sinkId) {
      try {
        await this._playCtx.setSinkId(this._sinkId);
      } catch (err) {
        if (!errorToastShown) {
          this._toast("Динамик недоступен");
          errorToastShown = true;
        }
      }
    }
    const nodes = document.querySelectorAll("audio");
    for (let i = 0; i < nodes.length; i += 1) {
      const node = nodes[i];
      if (node.setSinkId && this._sinkId) {
        try {
          await node.setSinkId(this._sinkId);
        } catch (err) {
          if (!errorToastShown) {
            this._toast("Динамик недоступен");
            errorToastShown = true;
          }
        }
      }
    }
  },

  _toast(text, action) {
    const box = this._roomEls && this._roomEls.toastEl;
    if (!box) {
      this._setStatus(text);
      return;
    }
    box.replaceChildren();
    const msg = document.createElement("span");
    msg.textContent = text;
    box.appendChild(msg);
    if (action && action.label && action.run) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "meeting-toast-action";
      btn.textContent = action.label;
      btn.addEventListener("click", () => {
        box.hidden = true;
        action.run();
      });
      box.appendChild(btn);
    }
    box.hidden = false;
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      box.hidden = true;
    }, TOAST_MS);
  },

  _speakingTile() {
    const box = this._roomEls && this._roomEls.tilesEl;
    return box ? box.querySelector(".meeting-tile-speaking") : null;
  },

  _firstTile() {
    const box = this._roomEls && this._roomEls.tilesEl;
    return box ? box.querySelector(".meeting-tile") : null;
  },

  _syncLayoutBtn() {
    const btn = this._roomEls && this._roomEls.layoutBtn;
    if (!btn) return;
    const speaker = this._layout === LAYOUT_SPEAKER;
    btn.setAttribute("aria-pressed", speaker ? "true" : "false");
    btn.setAttribute("aria-label", speaker ? "Сетка" : "Спикер");
    btn.title = speaker ? "Сетка" : "Спикер";
  },

  isImmersive() {
    const stage = this._roomEls && this._roomEls.stageEl;
    return Boolean(fullscreenElement() === stage || (stage && stage.classList.contains(IMMERSIVE_CLASS)));
  },

  async enterImmersive(tile) {
    const stage = this._roomEls && this._roomEls.stageEl;
    if (!stage) return;
    this._setSpotlight(tile || this._screenTile());
    stage.classList.add(IMMERSIVE_CLASS);
    document.body.classList.add("meeting-immersive");
    await enterNativeFullscreen(stage);
    this._syncFsChrome();
  },

  async exitImmersive() {
    this._clearSpotlight();
    const stage = this._roomEls && this._roomEls.stageEl;
    if (stage) stage.classList.remove(IMMERSIVE_CLASS);
    document.body.classList.remove("meeting-immersive");
    await exitNativeFullscreen();
    this._syncFsChrome();
  },

  async toggleStageFullscreen() {
    if (this.isImmersive()) await this.exitImmersive();
    else await this.enterImmersive(this._screenTile());
  },

  async toggleTileFullscreen(tile) {
    if (!tile) return;
    if (this.isImmersive() && tile.classList.contains("is-spotlight-tile")) {
      this._pinLocked = false;
      await this.exitImmersive();
      return;
    }
    this._layout = LAYOUT_SPEAKER;
    this._pinLocked = true;
    await this.enterImmersive(tile);
    this._syncLayoutBtn();
  },

  _setSpotlight(tile) {
    this._spotlightId = tile && tile.id ? tile.id : "";
    this._applySpotlight();
  },

  _clearSpotlight() {
    this._spotlightId = "";
    this._applySpotlight();
  },

  _applySpotlight() {
    const box = this._roomEls && this._roomEls.tilesEl;
    if (!box) return;
    const wanted = this._spotlightId;
    let found = false;
    Array.from(box.querySelectorAll(".meeting-tile")).forEach((node) => {
      const on = Boolean(wanted) && node.id === wanted;
      node.classList.toggle("is-spotlight-tile", on);
      const btn = node.querySelector("[data-tile-fs]");
      if (btn) {
        btn.innerHTML = on ? ICON_COMPRESS : ICON_EXPAND;
        btn.setAttribute("aria-label", on ? "Свернуть окно" : "Развернуть окно");
        btn.title = on ? "Свернуть" : "Развернуть";
      }
      if (on) found = true;
    });
    if (wanted && !found) this._spotlightId = "";
    box.classList.toggle("has-focus", Boolean(this._spotlightId));
  },

  _syncFsChrome() {
    const on = this.isImmersive();
    const btn = this._roomEls && this._roomEls.fsBtn;
    if (btn) {
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.setAttribute("aria-label", on ? "Свернуть экран" : "На весь экран");
      const text = btn.querySelector(".meeting-dock-label");
      if (text) text.textContent = on ? "Свернуть" : "На весь экран";
    }
    this._applySpotlight();
  },

  async _openPreview() {
    const els = this._preview.els;
    const picked = this.selectedDevices();
    if (this._preview.stream) {
      this._preview.stream.getTracks().forEach((track) => track.stop());
    }
    const listed = await listInputDevices();
    const opened = await openPreviewTracks(picked.audioDeviceId, picked.videoDeviceId, listed.video);
    this._preview.stream = opened.stream;
    this._preview.videoError = opened.videoError;
    await this._refreshDeviceLists();
    this._attachPreview(opened.stream, els, opened.videoError);
  },

  async _refreshDeviceLists() {
    const els = this._preview.els;
    if (!els) return;
    const lists = await listInputDevices();
    if (els.audioSelect) fillDeviceSelect(els.audioSelect, lists.audio, KIND_AUDIO_IN, els.audioSelect.value);
    if (els.videoSelect) fillDeviceSelect(els.videoSelect, lists.video, KIND_VIDEO_IN, els.videoSelect.value);
  },

  _attachPreview(stream, els, videoError) {
    const audioTrack = stream.getAudioTracks()[0];
    const videoTrack = stream.getVideoTracks()[0];
    if (els.videoEl) {
      els.videoEl.srcObject = videoTrack ? stream : null;
      if (videoTrack) els.videoEl.play().catch(() => undefined);
    }
    if (els.statusEl) {
      els.statusEl.textContent = "Выберите микрофон и камеру. Полоска растёт от голоса, в окне — картинка.";
    }
    if (els.camStatusEl) {
      els.camStatusEl.textContent = videoTrack
        ? "Камера работает."
        : mediaErrorText(videoError, "Камера");
    }
    if (els.micStatusEl) {
      els.micStatusEl.textContent = audioTrack
        ? "Говорите — полоска должна расти."
        : "Микрофон недоступен.";
    }
    this._startMeter(stream, els);
  },

  _startMeter(stream, els) {
    if (this._preview.meterTimer) cancelAnimationFrame(this._preview.meterTimer);
    const audioTrack = stream.getAudioTracks()[0];
    if (!audioTrack || !els.meterEl) return;
    const ctx = this._preview.audioCtx || new AudioContext();
    this._preview.audioCtx = ctx;
    if (this._meterSource) {
      try { this._meterSource.disconnect(); } catch (_) {}
    }
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = METER_FFT_SIZE;
    source.connect(analyser);
    this._meterSource = source;
    const buffer = new Uint8Array(analyser.fftSize);
    let lastHeardAt = 0;
    let lastLoud = null;
    const tick = () => {
      const ratio = meterRatio(analyser, buffer);
      const pct = Math.min(100, Math.round(ratio * METER_GAIN * 100));
      els.meterEl.style.width = pct + "%";
      if (els.meterWrap) els.meterWrap.setAttribute("aria-valuenow", String(pct));
      if (ratio >= METER_HEARD) lastHeardAt = Date.now();
      const loud = lastHeardAt > 0 && Date.now() - lastHeardAt < METER_HOLD_MS;
      if (els.micStatusEl && audioTrack && loud !== lastLoud) {
        lastLoud = loud;
        els.micStatusEl.textContent = loud
          ? "Микрофон слышно."
          : "Говорите — полоска должна расти.";
      }
      this._preview.meterTimer = requestAnimationFrame(tick);
    };
    this._preview.meterTimer = requestAnimationFrame(tick);
  },

  async _restartMic(dfn3On) {
    if (!this.room) return false;
    const picked = this.selectedDevices();
    const wasOn = this.room.localParticipant.isMicrophoneEnabled;
    await this._stopDfn3();
    if (!wasOn) return !dfn3On || canUseDfn3();
    try {
      await this.room.localParticipant.setMicrophoneEnabled(false);
      await this.room.localParticipant.setMicrophoneEnabled(
        true,
        micCaptureOpts(this._pickedMicId || picked.audioDeviceId, dfn3On),
      );
      this._watchLocalMic();
      return dfn3On ? this._startDfn3() : true;
    } catch (err) {
      this._setStatus("Микрофон: " + (err && err.message ? err.message : err));
      return false;
    }
  },

  async _ensureAudioContexts() {
    const resumed = [];
    if (this._playCtx && this._playCtx.state === "suspended") {
      resumed.push(this._playCtx.resume());
    }
    if (this._preview && this._preview.audioCtx && this._preview.audioCtx.state === "suspended") {
      resumed.push(this._preview.audioCtx.resume());
    }
    if (resumed.length) {
      await Promise.all(resumed.map((p) => Promise.resolve(p).catch(() => undefined)));
    }
  },

  async _startDfn3() {
    await this._stopDfn3();
    const track = localMicTrack(this.room);
    if (!track) return false;
    try {
      const mod = await import(DFN3_MODULE);
      const assets = await dfn3AssetConfig();
      if (typeof track.setProcessor === "function" && mod.DeepFilterNoiseFilterProcessor) {
        const filter = new mod.DeepFilterNoiseFilterProcessor({
          sampleRate: DFN3_SAMPLE_RATE,
          noiseReductionLevel: DFN3_LEVEL,
          enabled: true,
          assetConfig: assets,
        });
        await track.setProcessor(filter);
        this._dfn3 = { filter: filter, track: track, mode: "processor" };
        return true;
      }
      return this._startDfn3Replace(track, mod, assets);
    } catch (err) {
      await this._stopDfn3();
      return false;
    }
  },

  async _startDfn3Replace(track, mod, assets) {
    if (!mod.DeepFilterNet3Core || typeof track.replaceTrack !== "function") return false;
    const raw = track.mediaStreamTrack;
    if (!raw) return false;
    const core = new mod.DeepFilterNet3Core({
      sampleRate: DFN3_SAMPLE_RATE,
      noiseReductionLevel: DFN3_LEVEL,
      assetConfig: assets,
    });
    await core.initialize();
    const ctx = new AudioContext({ sampleRate: DFN3_SAMPLE_RATE });
    const node = await core.createAudioWorkletNode(ctx);
    const src = ctx.createMediaStreamSource(new MediaStream([raw]));
    const dest = ctx.createMediaStreamDestination();
    src.connect(node).connect(dest);
    await track.replaceTrack(dest.stream.getAudioTracks()[0]);
    this._dfn3 = { core: core, ctx: ctx, node: node, src: src, track: track, raw: raw, mode: "replace" };
    return true;
  },

  async _stopDfn3() {
    const bag = this._dfn3;
    this._dfn3 = null;
    if (!bag) return;
    try {
      if (bag.mode === "processor" && bag.track && bag.track.setProcessor) {
        await bag.track.setProcessor(null);
      }
      if (bag.filter && bag.filter.destroy) await bag.filter.destroy();
      if (bag.src) bag.src.disconnect();
      if (bag.node) bag.node.disconnect();
      if (bag.core && bag.core.destroy) bag.core.destroy();
      if (bag.ctx) await bag.ctx.close().catch(() => undefined);
      if (bag.mode === "replace" && bag.track && bag.raw && bag.track.replaceTrack) {
        await bag.track.replaceTrack(bag.raw);
      }
    } catch (err) {
      return;
    }
  },

  async _stopVoice() {
    await this._stopDfn3();
    this._stopMicLevelWatch();
    if (!this._playCtx) return;
    await this._playCtx.close().catch(() => undefined);
    this._playCtx = null;
    this._playComp = null;
  },

  _ensurePlayback() {
    if (this._playCtx) return this._playCtx;
    if (!canUseLoudness()) return null;
    const ctx = new AudioContext();
    const comp = ctx.createDynamicsCompressor();
    comp.threshold.value = LOUDNESS_THRESHOLD;
    comp.knee.value = LOUDNESS_KNEE;
    comp.ratio.value = LOUDNESS_RATIO;
    comp.attack.value = LOUDNESS_ATTACK;
    comp.release.value = LOUDNESS_RELEASE;
    const gain = ctx.createGain();
    gain.gain.value = LOUDNESS_GAIN;
    comp.connect(gain).connect(ctx.destination);
    this._playCtx = ctx;
    this._playComp = comp;
    if (this._sinkId && ctx.setSinkId) ctx.setSinkId(this._sinkId).catch(() => undefined);
    if (ctx.state === "suspended") ctx.resume().catch(() => undefined);
    return ctx;
  },

  _setStatus(text) {
    if (this._roomEls && this._roomEls.statusEl) this._roomEls.statusEl.textContent = text;
  },

  _syncControls(els) {
    if (!els || !this.room) return;
    const local = this.room.localParticipant;
    if (this._joined && !this._rejoining) {
      this._lastMedia = { mic: local.isMicrophoneEnabled, cam: local.isCameraEnabled };
    }
    this._markToggle(els.micBtn, local.isMicrophoneEnabled, "Микрофон");
    this._markToggle(els.camBtn, local.isCameraEnabled, "Камера");
    this._refreshCamSwitch();
    this._markToggle(els.screenBtn, local.isScreenShareEnabled, "Экран");
    if (els.screenBtn) {
      if (isPhoneClient()) {
        els.screenBtn.hidden = true;
      } else {
        const allowed = canShareScreen() || local.isScreenShareEnabled;
        els.screenBtn.disabled = !allowed;
        els.screenBtn.title = allowed ? "" : SCREEN_HINT;
      }
    }
    this._syncFsChrome();
    this._syncLayoutBtn();
    if (els.soundBtn) this._markToggle(els.soundBtn, this._soundOn, "Звук");
    if (els.denoiseBtn) {
      els.denoiseBtn.hidden = false;
      this._markToggle(els.denoiseBtn, this._serverDenoise, "Шумодав");
    }
  },

  _markToggle(btn, on, label) {
    if (!btn) return;
    btn.classList.toggle("is-off", !on);
    btn.classList.toggle("is-on", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.setAttribute("aria-label", (on ? "Выключить " : "Включить ") + label.toLowerCase());
    const text = btn.querySelector(".meeting-dock-label");
    if (text) text.textContent = label;
  },

  _screenTile() {
    const box = this._roomEls && this._roomEls.tilesEl;
    return box ? box.querySelector(".meeting-tile-screen.is-lead") || box.querySelector(".meeting-tile-screen") : null;
  },

  pinScreenLead() {
    const lead = this._screenTile();
    if (!lead) return;
    this._pinLocked = false;
    this._spotlightId = lead.id;
    this._applySpotlight();
    if (this.room && this._roomEls) this._renderTiles(this.room, this._roomEls);
  },

  _collectScreens(people) {
    const screens = [];
    people.forEach((person) => {
      person.trackPublications.forEach((pub) => {
        if (pub.kind !== "video" || !isScreenPub(pub)) return;
        const sid = String(pub.trackSid || pub.sid || "");
        if (!sid) return;
        screens.push({ sid: sid, personId: tileKey(person), person: person, pub: pub });
      });
    });
    return screens;
  },

  _ensureScreenTile(tilesEl, screen) {
    const id = SCREEN_TILE_PREFIX + screen.sid;
    let tile = document.getElementById(id);
    if (tile) return tile;
    tile = document.createElement("article");
    tile.id = id;
    tile.className = "meeting-tile meeting-tile-screen";
    tile.setAttribute("role", "listitem");
    tile.innerHTML =
      '<div class="meeting-tile-stage"></div><div class="meeting-tile-meta"><span class="meeting-tile-name"></span><span class="meeting-tile-flags"></span></div><button type="button" class="meeting-tile-fs" data-tile-fs="1" aria-label="Развернуть окно" title="Развернуть">' +
      ICON_EXPAND +
      "</button>";
    tilesEl.appendChild(tile);
    return tile;
  },

  _fillScreenTile(tile, screen, lead) {
    tile.classList.toggle("is-lead", Boolean(lead));
    const nameEl = tile.querySelector(".meeting-tile-name");
    nameEl.textContent = TEXT_SCREEN_PREFIX + this._label(screen.person);
    const stage = tile.querySelector(".meeting-tile-stage");
    const pub = screen.pub;
    const key = pub && pub.track ? String(pub.track.sid || pub.trackSid || screen.sid) : "";
    if (stage.dataset.videoKey === key) return;
    stage.dataset.videoKey = key;
    stage.replaceChildren();
    if (!pub || !pub.track) {
      const mark = document.createElement("div");
      mark.className = "meeting-tile-placeholder";
      mark.textContent = "Э";
      stage.appendChild(mark);
      return;
    }
    const node = pub.track.attach();
    markInlineVideo(node);
    node.muted = true;
    node.className = "meeting-tile-stream is-screen";
    stage.appendChild(node);
  },

  _onMicSilent() {
    const local = this.room && this.room.localParticipant;
    if (local && local.setAttributes) {
      local.setAttributes({ mic_silent: "1" });
    }
    this._toast(TEXT_MIC_SILENT, {
      label: TEXT_MIC_RESTART,
      run: () => this._restartMic(this._dfn3On),
    });
    if (this.room && this._roomEls) this._renderTiles(this.room, this._roomEls);
  },

  _clearMicSilent() {
    const local = this.room && this.room.localParticipant;
    if (local && local.setAttributes) {
      local.setAttributes({ mic_silent: "" });
    }
  },

  _watchLocalMic() {
    const track = localMicTrack(this.room);
    const media = track && track.mediaStreamTrack;
    if (!media) return;
    // Используем addEventListener вместо прямой перезаписи onmute/onended
    media.addEventListener("mute", () => this._onMicSilent());
    media.addEventListener("ended", () => this._onMicSilent());
    this._watchMicLevel(media);
  },

  _stopMicLevelWatch() {
    if (this._silentMeterTimer) {
      cancelAnimationFrame(this._silentMeterTimer);
      this._silentMeterTimer = 0;
    }
    if (this._silentMeterSource) {
      try { this._silentMeterSource.disconnect(); } catch (_) {}
      this._silentMeterSource = null;
    }
    if (this._silentMeterCtx) {
      try { this._silentMeterCtx.close(); } catch (_) {}
      this._silentMeterCtx = null;
    }
  },

  _watchMicLevel(media) {
    this._stopMicLevelWatch();
    try {
      const ctx = new AudioContext();
      this._silentMeterCtx = ctx;
      const source = ctx.createMediaStreamSource(new MediaStream([media]));
      this._silentMeterSource = source;
      const analyser = ctx.createAnalyser();
      analyser.fftSize = METER_FFT_SIZE;
      source.connect(analyser);
      const buffer = new Uint8Array(analyser.fftSize);
      const tick = () => {
        if (!this.room || !this.room.localParticipant || !this.room.localParticipant.isMicrophoneEnabled) {
          this._stopMicLevelWatch();
          return;
        }
        if (meterRatio(analyser, buffer) >= METER_HEARD) {
          this._clearMicSilent();
        }
        this._silentMeterTimer = requestAnimationFrame(tick);
      };
      this._silentMeterTimer = requestAnimationFrame(tick);
    } catch (e) {
      // Игнорируем в случае ограничений аудиоконтекста
    }
  },

  _maybePwaHint() {
    if (!isPhoneClient()) return;
    try {
      if (localStorage.getItem(PWA_HINT_KEY)) return;
      localStorage.setItem(PWA_HINT_KEY, "1");
    } catch (err) {
      return;
    }
    this._toast(TEXT_PWA_HINT);
  },

  _bindChrome(els) {
    if (!els || els._chromeBound) return;
    els._chromeBound = true;
    if (els.layoutBtn) els.layoutBtn.addEventListener("click", () => this.toggleLayout());
    if (els.rosterBtn) els.rosterBtn.addEventListener("click", () => this.toggleRoster());
    if (els.copyBtn) els.copyBtn.addEventListener("click", () => this.copyInvite());
    if (els.endBtn) els.endBtn.addEventListener("click", () => this.endForAll());
    if (els.moreBtn) els.moreBtn.addEventListener("click", () => this.toggleMore());
    if (els.speakerBtn) {
      els.speakerBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        this.toggleSpeakerMenu();
      });
    }
    if (els.speakerMenu) {
      els.speakerMenu.addEventListener("click", (event) => event.stopPropagation());
    }
    document.addEventListener("click", () => this.toggleSpeakerMenu(false));
    if (els.soundBtn) els.soundBtn.addEventListener("click", () => this.toggleSound());
    if (els.screenLeadBtn) els.screenLeadBtn.addEventListener("click", () => this.pinScreenLead());
    if (els.denoiseBtn) {
      els.denoiseBtn.hidden = false;
      els.denoiseBtn.addEventListener("click", () => this.toggleDenoise());
    }
    if (els.rosterBackdrop) {
      els.rosterBackdrop.addEventListener("click", () => this.toggleRoster(false));
    }
    if (els.rosterClose) els.rosterClose.addEventListener("click", () => this.toggleRoster(false));
  },

  _bind(room, els) {
    const LK = window.LivekitClient;
    this._roomEls = els;
    const redraw = () => {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => {
        this._raf = 0;
        this._dropRawIfCleaner(room);
        this._renderTiles(room, els);
        this._fillRoster(
          this._participants(room).filter((person) => !isHidden(person)),
          room,
        );
      });
    };
    const onPublished = (pub, person) => {
      const sid = String((pub && (pub.trackSid || pub.sid)) || "");
      if (sid) this._publishedAt[sid] = Date.now();
      if (
        !this._soundOn &&
        pub &&
        pub.kind === "audio" &&
        person !== room.localParticipant &&
        pub.setSubscribed
      ) {
        pub.setSubscribed(false);
      }
      if (pub && isScreenPub(pub)) {
        this._pinLocked = false;
        this._spotlightId = SCREEN_TILE_PREFIX + sid;
      }
      redraw();
    };
    room.on(LK.RoomEvent.ParticipantConnected, (person) => {
      if (!isHidden(person) && this._serverDenoise === false) this._publishDenoise(false);
      redraw();
    });
    room.on(LK.RoomEvent.ParticipantDisconnected, redraw);
    if (LK.RoomEvent.TrackPublished) room.on(LK.RoomEvent.TrackPublished, onPublished);
    if (LK.RoomEvent.TrackUnpublished) room.on(LK.RoomEvent.TrackUnpublished, redraw);
    if (LK.RoomEvent.TrackStreamStateChanged) room.on(LK.RoomEvent.TrackStreamStateChanged, redraw);
    if (LK.RoomEvent.ParticipantNameChanged) room.on(LK.RoomEvent.ParticipantNameChanged, redraw);
    if (LK.RoomEvent.ParticipantAttributesChanged) {
      room.on(LK.RoomEvent.ParticipantAttributesChanged, redraw);
    }
    if (LK.RoomEvent.ConnectionQualityChanged) {
      room.on(LK.RoomEvent.ConnectionQualityChanged, (quality, person) => {
        if (person && person.identity) this._quality[person.identity] = quality;
        if (person === room.localParticipant) this._linkQuality(quality);
        redraw();
      });
    }
    // ВАЖНО: Не спамим предупреждением о сломанном микрофоне при обычном молчании пользователя
    if (LK.RoomEvent.MediaDevicesError) {
      room.on(LK.RoomEvent.MediaDevicesError, (err) => {
        this._toast(mediaErrorText(err, "Микрофон"));
        this._onMicSilent();
      });
    }
    if (LK.RoomEvent.ParticipantMetadataChanged) {
      room.on(LK.RoomEvent.ParticipantMetadataChanged, () => this._reattachAudio());
    }
    if (LK.RoomEvent.DataReceived) {
      room.on(LK.RoomEvent.DataReceived, (payload, person, _kind, topic) => {
        if (topic && topic !== DENOISE_TOPIC) return;
        const raw =
          payload && payload.byteLength !== undefined
            ? new TextDecoder().decode(payload)
            : payload;
        const on = parseDenoisePayload(raw);
        if (on === null || !person || !person.identity) return;
        this._denoisePrefs[person.identity] = on;
        this._reattachAudio();
      });
    }
    room.on(LK.RoomEvent.LocalTrackPublished, onPublished);
    room.on(LK.RoomEvent.LocalTrackUnpublished, redraw);
    room.on(LK.RoomEvent.TrackMuted, () => {
      this._syncControls(els);
      this._renderTiles(room, els);
    });
    room.on(LK.RoomEvent.TrackUnmuted, () => {
      this._syncControls(els);
      this._renderTiles(room, els);
    });
    room.on(LK.RoomEvent.ActiveSpeakersChanged, () => this._syncSpeaking(room));
    if (LK.RoomEvent.Reconnecting) {
      room.on(LK.RoomEvent.Reconnecting, () => this._netStatus(TEXT_LINK_LOST, true));
    }
    if (LK.RoomEvent.Reconnected) {
      room.on(LK.RoomEvent.Reconnected, () => {
        this._resetLinkQuality();
        this._netStatus(TEXT_LINK_BACK, false);
        this._toast(TEXT_LINK_BACK);
        this._reattachAudio();
        redraw();
      });
    }
    if (LK.RoomEvent.ActiveDeviceChanged) {
      room.on(LK.RoomEvent.ActiveDeviceChanged, (kind) => {
        if (kind === KIND_VIDEO_IN) this._syncLocalMirror();
        if (kind === KIND_AUDIO_IN) this.refreshMics();
      });
    }
    room.on(LK.RoomEvent.Disconnected, (reason) => {
      this._joined = false;
      this._resetLinkQuality(0);
      this._stopAllTimers();
      const R = LK.DisconnectReason || {};
      const duplicate = R.DUPLICATE_IDENTITY !== undefined ? R.DUPLICATE_IDENTITY : 2;
      if (reason === duplicate) {
        this._showDuplicateSession(els);
        return;
      }
      if (this._leaving || reason === (R.CLIENT_INITIATED !== undefined ? R.CLIENT_INITIATED : 1)) return;
      const removed = R.PARTICIPANT_REMOVED !== undefined ? R.PARTICIPANT_REMOVED : 4;
      const deleted = R.ROOM_DELETED !== undefined ? R.ROOM_DELETED : 5;
      const closed = R.ROOM_CLOSED !== undefined ? R.ROOM_CLOSED : 10;
      if (reason === removed) {
        if (els.statusEl) els.statusEl.textContent = "Вас удалили из комнаты.";
        return;
      }
      if (els.statusEl && els.statusEl.textContent.startsWith("Не удалось войти")) return;
      // Комнату закрыли (встреча завершена) или связь пропала: возвращаемся, пока встреча идёт
      if (reason === deleted || reason === closed) {
        if (els.statusEl) els.statusEl.textContent = "Комната закрыта.";
      }
      this._beginRejoin(els);
    });
    room.on(LK.RoomEvent.TrackSubscribed, (track, pub, participant) => {
      if (participant === room.localParticipant && track.kind === "audio") return;
      this._attachTrack(els, participant, track, pub);
      redraw();
    });
    room.on(LK.RoomEvent.TrackUnsubscribed, (track) => {
      track.detach().forEach((node) => {
        if (node._skkkkPlaySrc) {
          try {
            node._skkkkPlaySrc.disconnect();
          } catch (err) {
            return;
          }
        }
        node.remove();
      });
      redraw();
    });
    if (els.rosterList && !els.rosterList.dataset.bound) {
      els.rosterList.dataset.bound = "1";
      els.rosterList.addEventListener("click", (event) => {
        const row = event.target.closest("[data-tile-id]");
        if (!row) return;
        this.focusParticipant(document.getElementById(row.dataset.tileId));
      });
    }
    if (!this._fsBound) {
      this._fsBound = true;
      const onFs = () => {
        const stage = this._roomEls && this._roomEls.stageEl;
        if (!fullscreenElement()) {
          if (stage) stage.classList.remove(IMMERSIVE_CLASS);
          document.body.classList.remove("meeting-immersive");
          this._clearSpotlight();
        }
        this._syncFsChrome();
      };
      document.addEventListener("fullscreenchange", onFs);
      document.addEventListener("webkitfullscreenchange", onFs);
      document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        this.toggleRoster(false);
        this.toggleMore(false);
        if (this.isImmersive() && !fullscreenElement()) {
          this.exitImmersive();
        }
      });
    }
  },

  _participants(room) {
    if (!room) return [];
    const list = room.localParticipant ? [room.localParticipant] : [];
    if (room.remoteParticipants && room.remoteParticipants.values) {
      return list.concat(Array.from(room.remoteParticipants.values()));
    }
    return list;
  },

  _label(person) {
    return person.name || person.identity || "Участник";
  },

  _renderTiles(room, els) {
    if (!els) return;
    const people = this._participants(room).filter((person) => !isHidden(person));
    if (els.peopleEl) {
      // Стабильный порядок по имени, длинный список — «первые + ещё N» с полным списком в подсказке.
      const names = people.map((person) => this._label(person)).sort((a, b) => a.localeCompare(b, "ru"));
      const shown = names.slice(0, 3).join(", ") + (names.length > 3 ? " и ещё " + (names.length - 3) : "");
      els.peopleEl.textContent = "В комнате (" + names.length + "): " + shown;
      els.peopleEl.title = names.join(", ");
    }
    if (!els.tilesEl) return;
    this._bindTileClicks(els.tilesEl);
    const screens = this._collectScreens(people);
    const order = window.SKKKKLayout.tileOrder(
      people.map((person) => ({ id: "meeting-tile-" + tileKey(person) })),
      screens,
      this._publishedAt,
    );
    const seen = new Set();
    order.forEach((item) => {
      if (item.kind === window.SKKKKLayout.KIND_SCREEN) {
        const screen = screens.find((row) => SCREEN_TILE_PREFIX + row.sid === item.id);
        if (!screen) return;
        const tile = this._ensureScreenTile(els.tilesEl, screen);
        seen.add(tile.id);
        this._fillScreenTile(tile, screen, item.lead);
        els.tilesEl.append(tile);
        return;
      }
      const person = people.find((row) => "meeting-tile-" + tileKey(row) === item.id);
      if (!person) return;
      const tile = this._ensureTile(els.tilesEl, person, person === room.localParticipant);
      seen.add(tile.id);
      this._fillTile(tile, person, room);
      els.tilesEl.append(tile);
    });
    Array.from(els.tilesEl.children).forEach((node) => {
      if (!seen.has(node.id)) node.remove();
    });
    const hasScreen = Boolean(els.tilesEl.querySelector(".meeting-tile-screen"));
    const lead = els.tilesEl.querySelector(".meeting-tile-screen.is-lead");
    if (lead && !this._pinLocked) this._spotlightId = lead.id;
    // Учитываем и людей, и экраны при расчете количества колонок сетки
    const totalTilesCount = people.length + screens.length;
    els.tilesEl.dataset.count = String(totalTilesCount);
    els.tilesEl.classList.toggle("is-dense", totalTilesCount >= 5);
    els.tilesEl.classList.toggle("is-screen-lead", isPhoneStage() && hasScreen && this._layout === LAYOUT_GRID);
    els.tilesEl.classList.toggle("is-spotlight", isPhoneStage() && hasScreen && this._layout === LAYOUT_SPEAKER);
    applyPhoneGrid(els.tilesEl, totalTilesCount);
    applyDesktopGrid(els.tilesEl, totalTilesCount);
    this._gridCount = totalTilesCount;
    if (!this._gridResizeBound) {
      this._gridResizeBound = true;
      window.addEventListener("resize", () => applyDesktopGrid(els.tilesEl, this._gridCount || 0));
    }
    if (this._layout === LAYOUT_SPEAKER && !this._pinLocked && !hasScreen) {
      const follow = this._speakingTile();
      if (follow) this._spotlightId = follow.id;
    }
    this._applySpotlight();
    this._fillRoster(people, room);
  },

  _syncSpeaking(room) {
    this._participants(room)
      .filter((person) => !isHidden(person))
      .forEach((person) => {
        const tile = document.getElementById("meeting-tile-" + tileKey(person));
        if (tile) tile.classList.toggle("meeting-tile-speaking", Boolean(person.isSpeaking));
      });
    if (this._layout === LAYOUT_SPEAKER && !this._pinLocked && !this._screenTile()) {
      const follow = this._speakingTile();
      if (follow) this._setSpotlight(follow);
    }
  },

  _fillRoster(people, room) {
    const list = this._roomEls && this._roomEls.rosterList;
    if (!list) return;
    list.replaceChildren();
    people.forEach((person) => {
      const tileId = "meeting-tile-" + tileKey(person);
      const row = document.createElement("button");
      row.type = "button";
      row.className = "meeting-roster-row";
      row.dataset.tileId = tileId;
      const name = document.createElement("span");
      name.className = "meeting-roster-name";
      name.textContent =
        this._label(person) + (person === room.localParticipant ? " (вы)" : "");
      const flags = document.createElement("span");
      flags.className = "meeting-roster-flags";
      flags.append(flagChip("mic", micFlag(person)), flagChip("cam", camFlag(person)));
      row.append(name, flags);
      list.appendChild(row);
    });
  },

  _bindPhoneGrid() {
    if (this._phoneGridBound) return;
    this._phoneGridBound = true;
    const mq = window.matchMedia("(orientation: landscape)");
    const onChange = () => {
      if (this.room && this._roomEls) this._renderTiles(this.room, this._roomEls);
    };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else mq.addListener(onChange);
  },

  _bindTileClicks(tilesEl) {
    if (tilesEl.dataset.bound === "1") return;
    tilesEl.dataset.bound = "1";
    tilesEl.addEventListener("click", (event) => {
      const btn = event.target.closest("[data-tile-fs]");
      if (btn) this.toggleTileFullscreen(btn.closest(".meeting-tile"));
    });
  },

  _ensureTile(tilesEl, person, isLocal) {
    const id = "meeting-tile-" + tileKey(person);
    let tile = document.getElementById(id);
    if (tile) return tile;
    tile = document.createElement("article");
    tile.id = id;
    tile.className = "meeting-tile" + (isLocal ? " meeting-tile-local" : "");
    tile.setAttribute("role", "listitem");
    tile.innerHTML =
      '<div class="meeting-tile-stage"></div><div class="meeting-tile-meta"><span class="meeting-tile-name"></span><span class="meeting-tile-flags"></span></div><button type="button" class="meeting-tile-fs" data-tile-fs="1" aria-label="Развернуть окно" title="Развернуть">' +
      ICON_EXPAND +
      "</button>";
    tilesEl.appendChild(tile);
    return tile;
  },

  _fillTile(tile, person, room) {
    const stage = tile.querySelector(".meeting-tile-stage");
    const nameEl = tile.querySelector(".meeting-tile-name");
    const flagsEl = tile.querySelector(".meeting-tile-flags");
    const isLocal = person === room.localParticipant;
    nameEl.textContent = this._label(person) + (isLocal ? " (вы)" : "");
    const mic = micFlag(person);
    const cam = camFlag(person);
    flagsEl.replaceChildren();
    flagsEl.appendChild(flagChip("mic", mic));
    flagsEl.appendChild(flagChip("cam", cam));
    // Качество связи у всех участников одинаково: палочки, как у нашего индикатора вверху
    const raw = this._quality[person.identity];
    const level = person === (this.room && this.room.localParticipant) && typeof this._sigLevel === "number" ? this._sigLevel : signalLevelOf(raw);
    if (level !== null) flagsEl.appendChild(signalBars(level));
    tile.classList.toggle("meeting-tile-mic-off", mic !== window.SKKKKLayout.FLAG_ON);
    tile.classList.toggle("meeting-tile-cam-off", cam !== window.SKKKKLayout.FLAG_ON);
    tile.classList.toggle("meeting-tile-speaking", Boolean(person.isSpeaking));
    this._paintVideo(stage, person);
  },

  _paintVideo(stage, person) {
    const pub = chosenVideoPub(person);
    const key = pub && pub.track ? String(pub.track.sid || pub.trackSid || "") : "";
    if (stage.dataset.videoKey === key) return;
    stage.dataset.videoKey = key;

    // Корректно отсоединяем предыдущие видеоэлементы
    const existingVideo = stage.querySelector("video");
    if (existingVideo && existingVideo.srcObject) {
      existingVideo.srcObject = null;
    }
    stage.replaceChildren();

    const tile = stage.closest(".meeting-tile");
    if (tile) tile.classList.toggle("meeting-tile-has-video", Boolean(pub && pub.track));
    if (!pub || !pub.track) {
      const mark = document.createElement("div");
      mark.className = "meeting-tile-placeholder";
      mark.textContent = this._label(person).slice(0, 1).toUpperCase();
      stage.appendChild(mark);
      return;
    }
    const node = pub.track.attach();
    markInlineVideo(node);
    node.muted = person === this.room.localParticipant;
    const isScreen = isScreenPub(pub);
    const mirror = person === this.room.localParticipant && !isScreen && isFrontCamera(pub.track.mediaStreamTrack);
    node.className = "meeting-tile-stream" + (isScreen ? " is-screen" : "") + (mirror ? " is-mirror" : "");
    stage.appendChild(node);
  },

  // Камеру переключили без смены трека в комнате: пересчитать зеркало у своего видео.
  _syncLocalMirror() {
    const room = this.room;
    if (!room || !room.localParticipant) return;
    const pub = chosenVideoPub(room.localParticipant);
    const track = pub && pub.track && pub.track.mediaStreamTrack;
    const front = isFrontCamera(track);
    document.querySelectorAll(".meeting-tile-local .meeting-tile-stream:not(.is-screen)").forEach((node) => {
      node.classList.toggle("is-mirror", front);
    });
  },

  _dropAttached(track) {
    if (!track || !track.detach) return;
    track.detach().forEach((node) => {
      if (node._skkkkPlaySrc) {
        try {
          node._skkkkPlaySrc.disconnect();
        } catch (err) {
          return;
        }
      }
      node.remove();
    });
  },

  _reattachAudio() {
    const els = this._roomEls;
    const room = this.room;
    if (!els || !room) return;
    this._dropRawIfCleaner(room);
    this._participants(room).forEach((person) => {
      if (person === room.localParticipant) return;
      person.trackPublications.forEach((pub) => {
        if (pub.track && pub.kind === "audio") this._attachTrack(els, person, pub.track, pub);
      });
    });
  },

  _dropRawIfCleaner(room) {
    if (!hasCleaner(room)) return;
    const prefs = this._denoisePrefs;
    room.remoteParticipants.forEach((person) => {
      if (isCleaner(person)) {
        person.trackPublications.forEach((pub) => {
          if (pub.kind !== "audio" || !pub.isSubscribed) return;
          if (!playCleanTrack(pub.track, room, prefs, pub)) this._dropAttached(pub.track);
        });
        return;
      }
      if (playRawAudio(person, room, prefs)) return;
      person.trackPublications.forEach((pub) => {
        if (pub.kind === "audio" && pub.track) this._dropAttached(pub.track);
      });
    });
  },

  _attachTrack(els, person, track, pub) {
    if (!els || !els.tilesEl) return;
    if (isHidden(person) && !isCleaner(person)) return;
    if (person === this.room.localParticipant && track.kind === "audio") return;
    if (track.kind === "audio" && !this._soundOn) return;
    if (isCleaner(person) && track.kind !== "audio") return;
    const prefs = this._denoisePrefs;
    if (track.kind === "audio" && isCleaner(person) && !playCleanTrack(track, this.room, prefs, pub)) return;
    if (track.kind === "audio" && !isCleaner(person) && !playRawAudio(person, this.room, prefs)) return;
    if (track.kind === "audio" && isCleaner(person)) {
      const identity = cleanSourceId(track, pub);
      const source = findPerson(this.room, identity);
      if (source) {
        source.trackPublications.forEach((item) => {
          if (item.kind === "audio" && item.track) this._dropAttached(item.track);
        });
      }
    }
    const host = isCleaner(person)
      ? els.stageEl || els.tilesEl
      : this._ensureTile(els.tilesEl, person, person === this.room.localParticipant);
    if (track.kind === "audio") this._dropAttached(track);
    const node = track.attach();
    if (track.kind === "audio") {
      node.autoplay = true;
      node.className = "visually-hidden";
      host.appendChild(node);
      if (this._sinkId && node.setSinkId) node.setSinkId(this._sinkId).catch(() => undefined);
      return;
    }
    this._paintVideo(host.querySelector(".meeting-tile-stage"), person);
  },
};

function tileKey(person) {
  // Стабильный ключ: используем identity в первую очередь, чтобы ID плитки не прыгал после авторизации
  return String(person.identity || person.sid || "unknown").replace(/[^A-Za-z0-9_-]/g, "-");
}

function isScreenPub(pub) {
  return Boolean(pub && String(pub.source || "").toLowerCase().indexOf("screen") >= 0);
}

function chosenVideoPub(person) {
  let camera = null;
  person.trackPublications.forEach((pub) => {
    if (pub.kind !== "video" || !pub.track || isScreenPub(pub)) return;
    if (!pub.isMuted && !camera) camera = pub;
  });
  return camera;
}

function findKindPub(person, kind, screen) {
  let found = null;
  person.trackPublications.forEach((pub) => {
    if (pub.kind !== kind || found) return;
    if (screen && !isScreenPub(pub)) return;
    if (!screen && isScreenPub(pub)) return;
    found = pub;
  });
  return found;
}

function micPub(person) {
  return findKindPub(person, "audio", false);
}

function camPub(person) {
  return findKindPub(person, "video", false);
}

function micFlag(person) {
  return window.SKKKKLayout.flagState(micPub(person), (person && person.attributes) || {});
}

function camFlag(person) {
  return window.SKKKKLayout.flagState(camPub(person), {});
}

function flagChip(kind, state) {
  const chip = document.createElement("span");
  chip.className = "meeting-chip is-" + state;
  const isMic = kind === "mic";
  let text = isMic ? TEXT_MIC_ON : TEXT_CAM_ON;
  if (state === window.SKKKKLayout.FLAG_NONE) text = isMic ? TEXT_NO_MIC : TEXT_NO_CAM;
  else if (state === window.SKKKKLayout.FLAG_OFF) text = isMic ? TEXT_MIC_OFF : TEXT_CAM_OFF;
  else if (state === window.SKKKKLayout.FLAG_SILENT) text = TEXT_MIC_SILENT_CHIP;
  chip.setAttribute("aria-label", text);
  chip.title = text;
  const on = state === window.SKKKKLayout.FLAG_ON;
  chip.innerHTML = isMic ? (on ? ICON_MIC : ICON_MIC_OFF) : on ? ICON_CAM : ICON_CAM_OFF;
  return chip;
}

function signalLevelOf(quality) {
  const key = String(quality && typeof quality === "object" ? quality.quality || quality.value || "" : quality || "").toLowerCase();
  return key in SIGNAL_LEVELS ? SIGNAL_LEVELS[key] : null;
}

// Те же палочки, что и вверху комнаты, но компактные, на плитке участника
function signalBars(level) {
  const box = document.createElement("span");
  box.className = "meeting-signal is-tile";
  box.dataset.level = String(level);
  const text = "Качество связи: " + SIGNAL_LABELS[level];
  box.setAttribute("role", "img");
  box.setAttribute("aria-label", text);
  box.title = text;
  for (let n = 0; n < 4; n += 1) box.appendChild(document.createElement("i"));
  return box;
}

window.SKKKKMeeting = SKKKKMeeting;
