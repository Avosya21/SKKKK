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
  return raw.replace(".", ":");
}

function identitiesMatch(left, right) {
  if (!left || !right) return false;
  return left === right || left.replace(":", ".") === right.replace(":", ".");
}

function cleanSourceId(track, pub) {
  const name = trackPublishName(track, pub);
  return name.indexOf("clean-") === 0 ? decodeCleanIdentity(name.slice(6)) : "";
}

function findPerson(room, identity) {
  if (!room || !identity) return null;
  if (room.localParticipant && room.localParticipant.identity === identity) {
    return room.localParticipant;
  }
  let found = null;
  if (room.remoteParticipants && room.remoteParticipants.forEach) {
    room.remoteParticipants.forEach((person) => {
      if (person.identity === identity) found = person;
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
  const all = await navigator.mediaDevices.enumerateDevices();
  return {
    audio: all.filter((item) => item.kind === KIND_AUDIO_IN),
    video: all.filter((item) => item.kind === KIND_VIDEO_IN),
  };
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
  room.localParticipant.trackPublications.forEach((pub) => {
    const source = String(pub.source || "").toLowerCase();
    if (!found && pub.kind === "audio" && pub.track && source.indexOf("screen") < 0) {
      found = pub.track;
    }
  });
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
  if (name === "NotFoundError") return kind + " не видна Windows. Проверьте Параметры → Конфиденциальность.";
  if (name === "NotReadableError") return kind + " занята другим приложением. Закройте Zoom или Телемост.";
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
  _dfn3On: false,
  _dfn3: null,
  _serverDenoise: true,
  _soundOn: true,
  _denoisePrefs: {},
  _playCtx: null,
  _playComp: null,
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
    const room = new LK.Room({
      adaptiveStream: true,
      dynacast: true,
      disconnectOnPageLeave: true,
      audioCaptureDefaults: {
        echoCancellation: true,
        autoGainControl: true,
        noiseSuppression: !canUseDfn3(),
      },
    });
    this.room = room;
    this._bind(room, els);
    this._bindChrome(els);
    if (els.screenBtn && !canShareScreen()) {
      els.screenBtn.disabled = true;
      els.screenBtn.title = SCREEN_HINT;
    }
    statusEl.textContent = "Подключаюсь к комнате…";
    try {
      await Promise.race([
        room.connect(livekit.url, livekit.token, { peerConnectionTimeout: ICE_TIMEOUT_MS }),
        new Promise((_, reject) => {
          setTimeout(() => reject(new Error(ICE_HINT)), ICE_TIMEOUT_MS);
        }),
      ]);
      this._joined = true;
      this._serverDenoise = true;
      this._soundOn = true;
      this._denoisePrefs = {};
      this._dfn3On = false;
      const audioOpts = micCaptureOpts(devices && devices.audioDeviceId, this._dfn3On);
      const videoOpts = devices && devices.videoDeviceId ? { deviceId: devices.videoDeviceId } : undefined;
      await room.localParticipant.setMicrophoneEnabled(true, audioOpts);
      if (this._dfn3On && !(await this._startDfn3())) {
        this._dfn3On = false;
        await this._restartMic(false);
        this._toast(DFN3_HINT);
      }
      await room.localParticipant.setCameraEnabled(Boolean(videoOpts), videoOpts);
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
      this.refreshSpeakers();
      this.watchEnded(els);
    } catch (err) {
      statusEl.textContent = "Не удалось войти в комнату: " + (err && err.message ? err.message : err);
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
        next ? micCaptureOpts(picked.audioDeviceId, this._dfn3On) : undefined,
      );
      if (next && this._dfn3On && !(await this._startDfn3())) {
        this._dfn3On = false;
        await this._restartMic(false);
        this._toast(DFN3_HINT);
      }
    } catch (err) {
      this._setStatus("Микрофон: " + (err && err.message ? err.message : err));
    }
    this._syncControls(this._roomEls);
    this._renderTiles(this.room, this._roomEls);
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

  async toggleScreen(btn) {
    if (!this.room) return;
    const on = this.room.localParticipant.isScreenShareEnabled;
    if (!on && !canShareScreen()) {
      this._setStatus(SCREEN_HINT);
      this._syncControls(this._roomEls);
      return;
    }
    try {
      await this.room.localParticipant.setScreenShareEnabled(!on);
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
    if (this._endedTimer) {
      clearInterval(this._endedTimer);
      this._endedTimer = 0;
    }
    await this._stopVoice();
    if (this.room) {
      await this.room.disconnect();
      this.room = null;
    }
    const url = this._roomEls && this._roomEls.leaveUrl;
    window.location.href = url || window.location.href;
  },

  async endForAll() {
    const url = this._roomEls && this._roomEls.endUrl;
    if (!url) return;
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
    if (!token || this._endedTimer) return;
    this._endedTimer = setInterval(async () => {
      try {
        const res = await fetch("/api/meetings/public/" + encodeURIComponent(token));
        const data = await res.json();
        if (data.ended) {
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
      this._toast("Ссылку гостя возьмите в карточке встречи.");
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
      wrap.hidden = true;
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
    this._applyOutputMute();
    this._syncControls(this._roomEls);
  },

  _applyOutputMute() {
    const root = (this._roomEls && this._roomEls.stageEl) || document;
    root.querySelectorAll("audio").forEach((node) => {
      node.muted = !this._soundOn;
    });
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
    if (this._playCtx && this._playCtx.setSinkId && this._sinkId) {
      try {
        await this._playCtx.setSinkId(this._sinkId);
      } catch (err) {
        this._toast("Динамик недоступен");
      }
    }
    const nodes = document.querySelectorAll("audio");
    for (let i = 0; i < nodes.length; i += 1) {
      const node = nodes[i];
      if (node.setSinkId && this._sinkId) {
        try {
          await node.setSinkId(this._sinkId);
        } catch (err) {
          this._toast("Динамик недоступен");
        }
      }
    }
  },

  _toast(text) {
    const box = this._roomEls && this._roomEls.toastEl;
    if (!box) {
      this._setStatus(text);
      return;
    }
    box.textContent = text;
    box.hidden = false;
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      box.hidden = true;
    }, 2200);
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
    if (this._meterSource) this._meterSource.disconnect();
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
        micCaptureOpts(picked.audioDeviceId, dfn3On),
      );
      return dfn3On ? this._startDfn3() : true;
    } catch (err) {
      this._setStatus("Микрофон: " + (err && err.message ? err.message : err));
      return false;
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
    this._markToggle(els.micBtn, local.isMicrophoneEnabled, "Микрофон");
    this._markToggle(els.camBtn, local.isCameraEnabled, "Камера");
    this._markToggle(els.screenBtn, local.isScreenShareEnabled, "Экран");
    if (els.screenBtn) {
      const allowed = canShareScreen() || local.isScreenShareEnabled;
      els.screenBtn.disabled = !allowed;
      els.screenBtn.title = allowed ? "" : SCREEN_HINT;
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
    return box ? box.querySelector(".meeting-tile-screen") : null;
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
      this._dropRawIfCleaner(room);
      this._renderTiles(room, els);
    };
    room.on(LK.RoomEvent.ParticipantConnected, (person) => {
      if (!isCleaner(person) && this._serverDenoise === false) this._publishDenoise(false);
      redraw();
    });
    room.on(LK.RoomEvent.ParticipantDisconnected, redraw);
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
    room.on(LK.RoomEvent.LocalTrackPublished, redraw);
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
    room.on(LK.RoomEvent.Disconnected, () => {
      this._joined = false;
      if (els.statusEl.textContent.startsWith("Не удалось войти")) {
        return;
      }
      els.statusEl.textContent = "Связь с комнатой оборвалась.";
    });
    room.on(LK.RoomEvent.TrackSubscribed, (track, pub, participant) => {
      if (participant === room.localParticipant && track.kind === "audio") return;
      this._attachTrack(els, participant, track, pub);
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
    return [room.localParticipant].concat(Array.from(room.remoteParticipants.values()));
  },

  _label(person) {
    return person.name || person.identity || "Участник";
  },

  _renderTiles(room, els) {
    if (!els) return;
    const people = this._participants(room).filter((person) => !isCleaner(person));
    if (els.peopleEl) {
      els.peopleEl.textContent = "В комнате: " + people.map((person) => this._label(person)).join(", ");
    }
    if (!els.tilesEl) return;
    this._bindTileClicks(els.tilesEl);
    const seen = new Set();
    people.forEach((person) => {
      const tile = this._ensureTile(els.tilesEl, person, person === room.localParticipant);
      seen.add(tile.id);
      this._fillTile(tile, person, room);
    });
    Array.from(els.tilesEl.children).forEach((node) => {
      if (!seen.has(node.id)) node.remove();
    });
    const hasScreen = Boolean(els.tilesEl.querySelector(".meeting-tile-screen"));
    els.tilesEl.dataset.count = String(people.length);
    els.tilesEl.classList.toggle("is-dense", people.length >= 5);
    els.tilesEl.classList.toggle("is-screen-lead", isPhoneStage() && hasScreen && this._layout === LAYOUT_GRID);
    els.tilesEl.classList.toggle("is-spotlight", isPhoneStage() && hasScreen && this._layout === LAYOUT_SPEAKER);
    applyPhoneGrid(els.tilesEl, people.length);
    if (this._layout === LAYOUT_SPEAKER && !this._pinLocked && !hasScreen) {
      const follow = this._speakingTile();
      if (follow) this._spotlightId = follow.id;
    }
    this._applySpotlight();
    this._fillRoster(people, room);
  },

  _syncSpeaking(room) {
    this._participants(room)
      .filter((person) => !isCleaner(person))
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
      flags.append(
        flagChip("mic", person.isMicrophoneEnabled),
        flagChip("cam", person.isCameraEnabled),
      );
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
    const micOn = person.isMicrophoneEnabled;
    const camOn = person.isCameraEnabled;
    flagsEl.replaceChildren();
    flagsEl.appendChild(flagChip("mic", micOn));
    flagsEl.appendChild(flagChip("cam", camOn));
    tile.classList.toggle("meeting-tile-mic-off", !micOn);
    tile.classList.toggle("meeting-tile-cam-off", !camOn);
    tile.classList.toggle("meeting-tile-speaking", Boolean(person.isSpeaking));
    this._paintVideo(stage, person);
  },

  _paintVideo(stage, person) {
    const pub = chosenVideoPub(person);
    const key = pub && pub.track ? String(pub.track.sid || pub.trackSid || "") : "";
    if (stage.dataset.videoKey === key) return;
    stage.dataset.videoKey = key;
    stage.replaceChildren();
    const tile = stage.closest(".meeting-tile");
    if (tile) tile.classList.toggle("meeting-tile-screen", Boolean(pub && isScreenPub(pub)));
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
    node.className = "meeting-tile-stream" + (isScreen ? " is-screen" : "");
    stage.appendChild(node);
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
          if (pub.kind !== "audio" || !pub.track) return;
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
    if (person === this.room.localParticipant && track.kind === "audio") return;
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
      node.muted = !this._soundOn;
      node.className = "visually-hidden";
      host.appendChild(node);
      if (this._sinkId && node.setSinkId) node.setSinkId(this._sinkId).catch(() => undefined);
      return;
    }
    this._paintVideo(host.querySelector(".meeting-tile-stage"), person);
  },
};

function tileKey(person) {
  return String(person.sid || person.identity || "unknown").replace(/[^A-Za-z0-9_-]/g, "-");
}

function isScreenPub(pub) {
  return Boolean(pub && String(pub.source || "").toLowerCase().indexOf("screen") >= 0);
}

function chosenVideoPub(person) {
  let screen = null;
  let camera = null;
  person.trackPublications.forEach((pub) => {
    if (pub.kind !== "video" || !pub.track) return;
    if (isScreenPub(pub)) {
      if (!screen) screen = pub;
      return;
    }
    if (!pub.isMuted && !camera) camera = pub;
  });
  return screen || camera;
}

function flagChip(kind, on) {
  const chip = document.createElement("span");
  chip.className = "meeting-chip" + (on ? " is-on" : " is-off");
  const name = kind === "mic" ? "Микрофон" : "Камера";
  const text = name + (on ? " включён" : " выключен");
  chip.setAttribute("aria-label", text);
  chip.title = text;
  chip.innerHTML = kind === "mic" ? (on ? ICON_MIC : ICON_MIC_OFF) : on ? ICON_CAM : ICON_CAM_OFF;
  return chip;
}

window.SKKKKMeeting = SKKKKMeeting;
