// Локальная запись комнаты на ПК сотрудника: композит плиток + микс звука → WebM/MP4.
// Не пишет на сервер и не трогает MEETING_RECORD / ASR. Ядро LiveKit-комнаты — только через фасад.
// Обрыв страницы: куски каждой секунды в IndexedDB; при следующем открытии портала — скачивание WebM.
(function (root) {
  if (root.__paLocalRecordBooted) return;
  root.__paLocalRecordBooted = true;
  const STATE_IDLE = "idle";
  const STATE_PICKING = "picking";
  const STATE_RECORDING = "recording";
  const STATE_STOPPING = "stopping";
  const STATE_ERROR = "error";
  const PRESENCE_TYPE = "local_recording";
  const HEARTBEAT_MS = 8000;
  const TIMESLICE_MS = 1000;
  const TARGET_FPS = 24;
  const CANVAS_W = 1920;
  const CANVAS_H = 1080;
  const VIDEO_BITRATE = 5_000_000;
  const AUDIO_BITRATE = 160_000;
  // Сначала WebM: Chrome isTypeSupported(mp4)=true, но MediaRecorder(canvas+audio, mp4)
  // часто сразу падает (пустой файл из проводника, запись не идёт). Firefox и так берёт WebM.
  // Seekable MP4 — через remux в конце, не через mime захвата.
  const MIME_CANDIDATES = [
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8,opus",
    "video/webm;codecs=vp9",
    "video/webm;codecs=vp8",
    "video/webm",
    "video/mp4;codecs=avc1.42E01E,mp4a.40.2",
    "video/mp4;codecs=avc1,mp4a.40.2",
    "video/mp4",
  ];

  function pickMimeType(isTypeSupported) {
    const check =
      typeof isTypeSupported === "function"
        ? isTypeSupported
        : (mime) =>
            typeof MediaRecorder !== "undefined" &&
            MediaRecorder.isTypeSupported &&
            MediaRecorder.isTypeSupported(mime);
    for (let i = 0; i < MIME_CANDIDATES.length; i += 1) {
      const mime = MIME_CANDIDATES[i];
      try {
        if (check(mime)) return mime;
      } catch (_) {
        /* ignore */
      }
    }
    return "";
  }

  function createMediaRecorder(stream, preferredMime) {
    const seen = {};
    const list = [];
    const push = (mime) => {
      const key = String(mime || "");
      if (seen[key]) return;
      seen[key] = true;
      list.push(mime || "");
    };
    push(preferredMime);
    for (let i = 0; i < MIME_CANDIDATES.length; i += 1) push(MIME_CANDIDATES[i]);
    push("");
    let lastErr = null;
    for (let i = 0; i < list.length; i += 1) {
      const mime = list[i];
      if (mime && MediaRecorder.isTypeSupported && !MediaRecorder.isTypeSupported(mime)) continue;
      try {
        const options = mime
          ? { mimeType: mime, videoBitsPerSecond: VIDEO_BITRATE, audioBitsPerSecond: AUDIO_BITRATE }
          : { videoBitsPerSecond: VIDEO_BITRATE, audioBitsPerSecond: AUDIO_BITRATE };
        const rec = new MediaRecorder(stream, options);
        return { recorder: rec, mime: rec.mimeType || mime || "" };
      } catch (err) {
        lastErr = err;
        if (!mime) continue;
        try {
          const rec = new MediaRecorder(stream, { mimeType: mime });
          return { recorder: rec, mime: rec.mimeType || mime };
        } catch (err2) {
          lastErr = err2;
        }
      }
    }
    throw lastErr || new Error("MediaRecorder недоступен");
  }

  function fileExt(mime) {
    return mime && String(mime).indexOf("mp4") >= 0 ? ".mp4" : ".webm";
  }

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  function buildFileName(meetingId, when, mime) {
    const d = when instanceof Date ? when : new Date();
    const stamp =
      d.getFullYear() +
      pad2(d.getMonth() + 1) +
      pad2(d.getDate()) +
      "-" +
      pad2(d.getHours()) +
      pad2(d.getMinutes()) +
      pad2(d.getSeconds());
    const id = meetingId == null || meetingId === "" ? "room" : String(meetingId);
    return "sozvon-" + id + "-" + stamp + fileExt(mime);
  }

  function parsePresencePayload(raw) {
    if (raw == null) return null;
    let text = raw;
    if (raw instanceof ArrayBuffer) text = new TextDecoder().decode(raw);
    else if (ArrayBuffer.isView(raw)) text = new TextDecoder().decode(raw);
    else if (typeof raw !== "string") return null;
    try {
      const data = JSON.parse(text);
      if (!data || data.type !== PRESENCE_TYPE) return null;
      return { on: Boolean(data.on), at: Number(data.at) || 0 };
    } catch (_) {
      return null;
    }
  }

  function supportsLocalRecording(globals) {
    const g = globals || (typeof window !== "undefined" ? window : globalThis);
    if (!g) return false;
    if (typeof g.MediaRecorder !== "function") return false;
    const proto =
      g.HTMLCanvasElement && g.HTMLCanvasElement.prototype
        ? g.HTMLCanvasElement.prototype
        : null;
    if (!proto || typeof proto.captureStream !== "function") return false;
    if (typeof g.isSecureContext === "boolean" && !g.isSecureContext) return false;
    return Boolean(
      pickMimeType(
        g.MediaRecorder && g.MediaRecorder.isTypeSupported
          ? (m) => g.MediaRecorder.isTypeSupported(m)
          : undefined,
      ),
    );
  }

  function drawCover(ctx, source, x, y, w, h) {
    const vw = source.videoWidth || source.naturalWidth || w;
    const vh = source.videoHeight || source.naturalHeight || h;
    if (vw < 2 || vh < 2 || w < 1 || h < 1) return;
    const scale = Math.max(w / vw, h / vh);
    const dw = vw * scale;
    const dh = vh * scale;
    const dx = x + (w - dw) / 2;
    const dy = y + (h - dh) / 2;
    ctx.drawImage(source, dx, dy, dw, dh);
  }

  // Экран целиком в кадр (без обрезки слева/справа), камеры — cover.
  function drawContain(ctx, source, x, y, w, h) {
    const vw = source.videoWidth || source.naturalWidth || w;
    const vh = source.videoHeight || source.naturalHeight || h;
    if (vw < 2 || vh < 2 || w < 1 || h < 1) return;
    const scale = Math.min(w / vw, h / vh);
    const dw = vw * scale;
    const dh = vh * scale;
    const dx = x + (w - dw) / 2;
    const dy = y + (h - dh) / 2;
    ctx.fillStyle = "#000000";
    ctx.fillRect(x, y, w, h);
    ctx.drawImage(source, dx, dy, dw, dh);
  }

  function drawSpeakingRing(ctx, x, y, w, h) {
    if (w < 8 || h < 8) return;
    const lw = Math.max(4, Math.min(w, h) * 0.014);
    ctx.strokeStyle = "#3d8bfd";
    ctx.lineWidth = lw;
    ctx.strokeRect(x + lw / 2, y + lw / 2, w - lw, h - lw);
  }

  function tilePortraitRadius(w, h) {
    // Как буква-заглушка и CSS .meeting-tile-avatar: круг по центру, не на всю плитку.
    return Math.max(18, Math.min(w, h) * 0.22);
  }

  function drawAvatar(ctx, source, x, y, w, h) {
    const iw = source.naturalWidth || source.width || 0;
    const ih = source.naturalHeight || source.height || 0;
    if (iw < 2 || ih < 2 || w < 1 || h < 1) return;
    const cx = x + w / 2;
    const cy = y + h / 2;
    const r = tilePortraitRadius(w, h);
    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.closePath();
    ctx.clip();
    const scale = Math.max((r * 2) / iw, (r * 2) / ih);
    const dw = iw * scale;
    const dh = ih * scale;
    ctx.drawImage(source, cx - dw / 2, cy - dh / 2, dw, dh);
    ctx.restore();
  }

  function drawPlaceholder(ctx, letter, x, y, w, h) {
    const cx = x + w / 2;
    const cy = y + h / 2;
    const r = tilePortraitRadius(w, h);
    ctx.fillStyle = "#2b3036";
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fill();
    const ch = String(letter || "?").slice(0, 1).toUpperCase();
    ctx.fillStyle = "#f3f4f6";
    ctx.font = "600 " + Math.round(r * 0.95) + "px system-ui,Segoe UI,sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(ch, cx, cy + 1);
    ctx.textAlign = "start";
    ctx.textBaseline = "alphabetic";
  }

  function drawNameBar(ctx, name, x, y, w, h) {
    const label = String(name || "").trim();
    if (!label || w < 40 || h < 36) return;
    const barH = Math.max(28, Math.min(44, h * 0.12));
    const pad = Math.max(8, barH * 0.28);
    ctx.fillStyle = "rgba(0,0,0,0.58)";
    ctx.fillRect(x, y + h - barH, w, barH);
    ctx.fillStyle = "#ffffff";
    ctx.font = "600 " + Math.round(Math.max(13, Math.min(20, barH * 0.48))) + "px system-ui,Segoe UI,sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(label, x + pad, y + h - barH / 2, w - pad * 2);
    ctx.textBaseline = "alphabetic";
  }

  function paintTiles(ctx, tilesEl, canvasW, canvasH) {
    ctx.fillStyle = "#111111";
    ctx.fillRect(0, 0, canvasW, canvasH);
    if (!tilesEl || typeof tilesEl.getBoundingClientRect !== "function") return;
    const box = tilesEl.getBoundingClientRect();
    if (box.width < 2 || box.height < 2) return;
    const sx = canvasW / box.width;
    const sy = canvasH / box.height;
    const tiles = tilesEl.querySelectorAll(".meeting-tile");
    for (let i = 0; i < tiles.length; i += 1) {
      const tile = tiles[i];
      const r = tile.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      const x = (r.left - box.left) * sx;
      const y = (r.top - box.top) * sy;
      const w = r.width * sx;
      const h = r.height * sy;
      ctx.save();
      ctx.beginPath();
      ctx.rect(x, y, w, h);
      ctx.clip();
      ctx.fillStyle = "#1a1d21";
      ctx.fillRect(x, y, w, h);

      const video = tile.querySelector("video");
      const avatar = tile.querySelector("img.meeting-tile-avatar");
      const placeholder = tile.querySelector(".meeting-tile-placeholder");
      const nameEl = tile.querySelector(".meeting-tile-name");
      const name = nameEl ? String(nameEl.textContent || "").trim() : "";

      const isScreen =
        (tile.classList && tile.classList.contains("meeting-tile-screen")) ||
        (video && video.classList && video.classList.contains("is-screen"));
      const speaking =
        tile.classList && tile.classList.contains("meeting-tile-speaking");

      let drawn = false;
      if (video && video.readyState >= 2 && (video.videoWidth || 0) > 1) {
        const mirror = video.classList && video.classList.contains("is-mirror");
        const drawVideo = isScreen ? drawContain : drawCover;
        if (mirror) {
          ctx.save();
          ctx.translate(x + w, y);
          ctx.scale(-1, 1);
          drawVideo(ctx, video, 0, 0, w, h);
          ctx.restore();
        } else {
          drawVideo(ctx, video, x, y, w, h);
        }
        drawn = true;
      } else if (avatar && avatar.complete && avatar.naturalWidth > 1) {
        drawAvatar(ctx, avatar, x, y, w, h);
        drawn = true;
      }
      if (!drawn) {
        const letter =
          (placeholder && placeholder.textContent) ||
          (name ? name.slice(0, 1) : "?");
        drawPlaceholder(ctx, letter, x, y, w, h);
      }
      drawNameBar(ctx, name, x, y, w, h);
      if (speaking) drawSpeakingRing(ctx, x, y, w, h);
      ctx.restore();
    }
  }

  // MediaRecorder WebM часто без Duration — штатный плеер Windows не мотает.
  // 1) патчим существующий float Duration (0x4489); 2) если элемента нет — вставляем в Info.
  function readVint(bytes, offset) {
    if (offset >= bytes.length) return null;
    const first = bytes[offset];
    let len = 1;
    let mask = 0x80;
    while (len <= 8 && (first & mask) === 0) {
      mask >>= 1;
      len += 1;
    }
    if (len > 8 || offset + len > bytes.length) return null;
    let value = first & (mask - 1);
    for (let i = 1; i < len; i += 1) value = (value << 8) | bytes[offset + i];
    return { length: len, value: value };
  }

  function writeVint(value) {
    if (value < 0) value = 0;
    let bytes = 1;
    while (bytes < 8 && value >= 1 << (bytes * 7)) bytes += 1;
    const out = new Uint8Array(bytes);
    for (let i = bytes - 1; i >= 0; i -= 1) {
      out[i] = value & 0xff;
      value >>= 8;
    }
    out[0] |= 1 << (8 - bytes);
    return out;
  }

  function durationElementBytes(durationSec) {
    const payload = new Uint8Array(8);
    new DataView(payload.buffer).setFloat64(0, durationSec);
    const out = new Uint8Array(2 + 1 + 8);
    out[0] = 0x44;
    out[1] = 0x89;
    out[2] = 0x88; // vint size = 8
    out.set(payload, 3);
    return out;
  }

  function patchExistingDuration(bytes, durationSec) {
    for (let i = 0; i < bytes.length - 10; i += 1) {
      if (bytes[i] !== 0x44 || bytes[i + 1] !== 0x89) continue;
      const size = readVint(bytes, i + 2);
      if (!size || size.value < 4 || size.value > 8) continue;
      const dataAt = i + 2 + size.length;
      if (dataAt + size.value > bytes.length) continue;
      const view = new DataView(bytes.buffer, bytes.byteOffset + dataAt, size.value);
      if (size.value === 4) view.setFloat32(0, durationSec);
      else if (size.value === 8) view.setFloat64(0, durationSec);
      else continue;
      return true;
    }
    return false;
  }

  function insertDurationIntoInfo(bytes, durationSec) {
    // Info EBML id: 0x1549A966
    for (let i = 0; i < bytes.length - 12; i += 1) {
      if (bytes[i] !== 0x15 || bytes[i + 1] !== 0x49 || bytes[i + 2] !== 0xa9 || bytes[i + 3] !== 0x66) {
        continue;
      }
      const size = readVint(bytes, i + 4);
      if (!size) continue;
      const contentAt = i + 4 + size.length;
      const unknown = size.length === 8 && size.value === 0x00ffffffffffffff;
      if (!unknown && size.value > bytes.length) continue;
      const contentEnd = unknown ? bytes.length : contentAt + size.value;
      if (contentEnd > bytes.length) continue;
      // Уже есть Duration внутри Info — не дублируем.
      for (let j = contentAt; j < contentEnd - 2; j += 1) {
        if (bytes[j] === 0x44 && bytes[j + 1] === 0x89) return null;
      }
      const dur = durationElementBytes(durationSec);
      const before = bytes.subarray(0, contentEnd);
      const after = bytes.subarray(contentEnd);
      if (unknown) {
        const merged = new Uint8Array(before.length + dur.length + after.length);
        merged.set(before, 0);
        merged.set(dur, before.length);
        merged.set(after, before.length + dur.length);
        return merged;
      }
      const newSize = writeVint(size.value + dur.length);
      const headEnd = i + 4;
      const merged = new Uint8Array(
        headEnd + newSize.length + size.value + dur.length + (bytes.length - contentEnd),
      );
      merged.set(bytes.subarray(0, headEnd), 0);
      merged.set(newSize, headEnd);
      merged.set(bytes.subarray(contentAt, contentEnd), headEnd + newSize.length);
      merged.set(dur, headEnd + newSize.length + size.value);
      merged.set(bytes.subarray(contentEnd), headEnd + newSize.length + size.value + dur.length);
      return merged;
    }
    return null;
  }

  function fixWebmDuration(blob, durationMs) {
    if (!blob || !(durationMs > 0) || String(blob.type || "").indexOf("webm") < 0) {
      return Promise.resolve(blob);
    }
    return blob.arrayBuffer().then((buffer) => {
      const durationSec = durationMs / 1000;
      let bytes = new Uint8Array(buffer.slice(0));
      if (patchExistingDuration(bytes, durationSec)) {
        return new Blob([bytes], { type: blob.type || "video/webm" });
      }
      const inserted = insertDurationIntoInfo(bytes, durationSec);
      if (!inserted) return blob;
      return new Blob([inserted], { type: blob.type || "video/webm" });
    });
  }

  // Куски MediaRecorder живут в куче вкладки, а MP4 собирается запросом, пока страница жива.
  // Закрытие, обновление и падение вкладки убивают и кучу, и remux. Каждая секунда пишется
  // в IndexedDB этого браузера; следующая страница портала собирает из неё WebM.
  // Убитый процесс браузера код не выполняет: остаётся только то, что транзакция уже зафиксировала.
  const CRASH_DB = "primaryanalitics-local-record";
  const CRASH_DB_VERSION = 1;
  const STORE_SESSIONS = "sessions";
  const STORE_CHUNKS = "chunks";
  const liveRecorders = [];
  let crashDbPromise = null;
  let crashWarnShown = false;
  let recovering = null;

  function crashDbExists() {
    const idb = root.indexedDB;
    if (!idb) return Promise.resolve(false);
    if (typeof idb.databases !== "function") return Promise.resolve(true);
    return idb
      .databases()
      .then((list) => (list || []).some((item) => item && item.name === CRASH_DB))
      .catch(() => true);
  }

  function openCrashDb() {
    return new Promise((resolve) => {
      try {
        const idb = root.indexedDB;
        if (!idb) {
          resolve(null);
          return;
        }
        const req = idb.open(CRASH_DB, CRASH_DB_VERSION);
        req.onupgradeneeded = () => {
          const db = req.result;
          if (!db.objectStoreNames.contains(STORE_SESSIONS)) {
            db.createObjectStore(STORE_SESSIONS, { keyPath: "id" });
          }
          if (!db.objectStoreNames.contains(STORE_CHUNKS)) {
            const store = db.createObjectStore(STORE_CHUNKS, { keyPath: "id" });
            store.createIndex("bySession", "sessionId", { unique: false });
          }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => resolve(null);
        req.onblocked = () => resolve(null);
      } catch (_) {
        resolve(null);
      }
    });
  }

  function crashDb() {
    if (!crashDbPromise) crashDbPromise = openCrashDb();
    return crashDbPromise;
  }

  function putStore(storeName, value) {
    return crashDb().then((db) => {
      if (!db) return false;
      return new Promise((resolve) => {
        try {
          const tx = db.transaction(storeName, "readwrite");
          tx.objectStore(storeName).put(value);
          tx.oncomplete = () => resolve(true);
          tx.onerror = () => resolve(false);
          tx.onabort = () => resolve(false);
        } catch (_) {
          resolve(false);
        }
      });
    });
  }

  function warnCrashWrite(api) {
    if (crashWarnShown) return;
    crashWarnShown = true;
    if (api && api.env && typeof api.env.toast === "function") {
      api.env.toast("Запасная копия записи не пишется. Если страница закроется, файл может пропасть.");
    }
  }

  function armCrashSession(api) {
    const stamp = Date.now().toString(36);
    const rnd = Math.random().toString(36).slice(2, 8);
    api.crashId = "s" + stamp + rnd;
    api.crashSeq = 0;
    const session = {
      id: api.crashId,
      meetingId: api.env && api.env.meetingId != null ? String(api.env.meetingId) : "",
      mime: "video/webm",
      fileName: buildFileName(api.env && api.env.meetingId, new Date(), "video/webm"),
      startedAt: api.startedAt || Date.now(),
      open: true,
    };
    api.crashWrite = putStore(STORE_SESSIONS, session).then((ok) => {
      if (!ok) warnCrashWrite(api);
      return ok;
    });
  }

  function rememberChunk(api, blob) {
    if (!api || !api.crashId || !blob || !blob.size) return;
    const seq = api.crashSeq;
    api.crashSeq += 1;
    const row = {
      id: api.crashId + ":" + String(seq).padStart(6, "0"),
      sessionId: api.crashId,
      seq: seq,
      blob: blob,
    };
    const prev = api.crashWrite || Promise.resolve();
    api.crashWrite = prev
      .catch(() => false)
      .then(() => putStore(STORE_CHUNKS, row))
      .then((ok) => {
        if (!ok) warnCrashWrite(api);
        return ok;
      });
  }

  function forgetCrashSession(id) {
    if (!id) return Promise.resolve();
    return crashDb().then((db) => {
      if (!db) return;
      return new Promise((resolve) => {
        try {
          const tx = db.transaction([STORE_SESSIONS, STORE_CHUNKS], "readwrite");
          const chunks = tx.objectStore(STORE_CHUNKS);
          tx.objectStore(STORE_SESSIONS).delete(id);
          if (root.IDBKeyRange && chunks.indexNames.contains("bySession")) {
            const req = chunks.index("bySession").openCursor(root.IDBKeyRange.only(id));
            req.onsuccess = () => {
              const cursor = req.result;
              if (!cursor) return;
              cursor.delete();
              cursor.continue();
            };
          }
          tx.oncomplete = () => resolve();
          tx.onerror = () => resolve();
          tx.onabort = () => resolve();
        } catch (_) {
          resolve();
        }
      });
    });
  }

  async function sealCrashSession(api) {
    if (!api || !api.crashId) return;
    const id = api.crashId;
    const pending = api.crashWrite;
    api.crashId = "";
    api.crashSeq = 0;
    api.crashWrite = Promise.resolve();
    try {
      if (pending) await pending;
    } catch (_) {
      /* копия уже не нужна: файл отдан браузеру */
    }
    await forgetCrashSession(id);
  }

  function allCrashSessions() {
    return crashDb().then((db) => {
      if (!db) return [];
      return new Promise((resolve) => {
        try {
          const req = db.transaction(STORE_SESSIONS, "readonly").objectStore(STORE_SESSIONS).getAll();
          req.onsuccess = () => resolve(req.result || []);
          req.onerror = () => resolve([]);
        } catch (_) {
          resolve([]);
        }
      });
    });
  }

  function chunksForSession(sessionId) {
    return crashDb().then((db) => {
      if (!db) return [];
      return new Promise((resolve) => {
        try {
          const req = db
            .transaction(STORE_CHUNKS, "readonly")
            .objectStore(STORE_CHUNKS)
            .index("bySession")
            .getAll(sessionId);
          req.onsuccess = () => resolve(req.result || []);
          req.onerror = () => resolve([]);
        } catch (_) {
          resolve([]);
        }
      });
    });
  }

  function crashFileName(session) {
    const raw = session && session.fileName ? String(session.fileName) : "";
    if (raw && /\.webm$/i.test(raw)) return raw;
    const when = session && session.startedAt ? new Date(session.startedAt) : new Date();
    return buildFileName(session && session.meetingId, when, "video/webm");
  }

  function triggerDownload(blob, fileName) {
    const doc = root.document;
    if (!doc || !doc.body || !blob) return false;
    const url = URL.createObjectURL(blob);
    const a = doc.createElement("a");
    a.href = url;
    a.download = fileName;
    a.rel = "noopener";
    doc.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
    return true;
  }

  function showCrashBanner(blob, fileName, sessionId) {
    const mount = () => {
      const doc = root.document;
      if (!doc || !doc.body || doc.getElementById("local-record-crash")) return;
      const bar = doc.createElement("div");
      bar.id = "local-record-crash";
      bar.className = "alert alert-warning";
      bar.setAttribute("role", "status");
      bar.setAttribute("aria-live", "polite");
      bar.style.cssText =
        "position:fixed;z-index:10050;left:1rem;right:1rem;top:1rem;display:flex;gap:0.75rem;align-items:center;justify-content:space-between;flex-wrap:wrap;margin:0;box-shadow:0 8px 24px rgba(0,0,0,.18);";
      const text = doc.createElement("p");
      text.style.cssText = "margin:0;flex:1 1 16rem;";
      text.textContent =
        "Запись созвона оборвалась. Файл сохранён в браузере как WebM, без перемотки. Если он уже скачался, нажмите «Не сохранять».";
      const actions = doc.createElement("div");
      actions.style.cssText = "display:flex;gap:0.5rem;flex-wrap:wrap;";
      const save = doc.createElement("button");
      save.type = "button";
      save.className = "btn btn-dark";
      save.setAttribute("data-act", "save");
      save.textContent = "Скачать WebM";
      const drop = doc.createElement("button");
      drop.type = "button";
      drop.className = "btn btn-outline-dark";
      drop.setAttribute("data-act", "dismiss");
      drop.textContent = "Не сохранять";
      const closeBar = () => {
        try {
          bar.remove();
        } catch (_) {
          /* ignore */
        }
      };
      save.addEventListener("click", () => {
        triggerDownload(blob, fileName);
        forgetCrashSession(sessionId).catch(() => {});
        closeBar();
      });
      drop.addEventListener("click", () => {
        forgetCrashSession(sessionId).catch(() => {});
        closeBar();
      });
      actions.appendChild(save);
      actions.appendChild(drop);
      bar.appendChild(text);
      bar.appendChild(actions);
      doc.body.appendChild(bar);
    };
    if (!root.document || !root.document.body) {
      try {
        root.document.addEventListener("DOMContentLoaded", mount, { once: true });
      } catch (_) {
        /* ignore */
      }
      return;
    }
    mount();
  }

  function sessionIsLive(id) {
    for (let i = 0; i < liveRecorders.length; i += 1) {
      const api = liveRecorders[i];
      if (api && api.crashId === id) return true;
    }
    return false;
  }

  function flushCrashOnHide() {
    for (let i = 0; i < liveRecorders.length; i += 1) {
      const api = liveRecorders[i];
      if (!api || (api.state !== STATE_RECORDING && api.state !== STATE_STOPPING)) continue;
      const rec = api.mediaRecorder;
      if (rec && rec.state === "recording") {
        try {
          rec.requestData();
        } catch (_) {
          /* последняя секунда может не успеть */
        }
      }
    }
  }

  function dropLiveRecorder(api) {
    const index = liveRecorders.indexOf(api);
    if (index >= 0) liveRecorders.splice(index, 1);
  }

  async function recoverCrashRecording() {
    if (recovering) return recovering;
    recovering = doRecover().finally(() => {
      recovering = null;
    });
    return recovering;
  }

  async function doRecover() {
    if (!(await crashDbExists())) return 0;
    const sessions = await allCrashSessions();
    let saved = 0;
    for (let i = 0; i < sessions.length; i += 1) {
      const session = sessions[i];
      if (!session || !session.id || sessionIsLive(session.id)) continue;
      const rows = await chunksForSession(session.id);
      rows.sort((a, b) => (a.seq || 0) - (b.seq || 0));
      const parts = [];
      for (let j = 0; j < rows.length; j += 1) {
        if (rows[j] && rows[j].blob && rows[j].blob.size) parts.push(rows[j].blob);
      }
      if (!parts.length) {
        await forgetCrashSession(session.id);
        continue;
      }
      const blob = new Blob(parts, { type: "video/webm" });
      const fileName = crashFileName(session);
      const key = "pa-local-record-crash:" + session.id;
      let offered = false;
      try {
        offered = root.sessionStorage && root.sessionStorage.getItem(key) === "1";
      } catch (_) {
        offered = false;
      }
      if (!offered) {
        try {
          if (root.sessionStorage) root.sessionStorage.setItem(key, "1");
        } catch (_) {
          /* повторное открытие вкладки скачает ещё раз */
        }
        triggerDownload(blob, fileName);
      }
      showCrashBanner(blob, fileName, session.id);
      saved += 1;
    }
    return saved;
  }

  function bootCrashRecovery() {
    try {
      root.addEventListener("pagehide", flushCrashOnHide);
    } catch (_) {
      /* ignore */
    }
    const kick = () => {
      recoverCrashRecording().catch(() => {});
    };
    try {
      if (root.document && root.document.readyState === "loading") {
        root.document.addEventListener("DOMContentLoaded", kick);
      } else {
        kick();
      }
    } catch (_) {
      kick();
    }
  }

  function createRecorder() {
    const api = {
      state: STATE_IDLE,
      env: null,
      mime: "",
      canvas: null,
      ctx: null,
      canvasStream: null,
      audioCtx: null,
      mixDest: null,
      mixSources: [],
      mediaRecorder: null,
      chunks: [],
      fileHandle: null,
      startedAt: 0,
      raf: 0,
      lastFrameAt: 0,
      paintPaused: false,
      heartbeatTimer: 0,
      subsPulseTimer: 0,
      visibilityHandler: null,
      trackIds: new Set(),
    };

    function setState(next) {
      api.state = next;
      if (api.env && typeof api.env.onState === "function") api.env.onState(next, api.isActive());
    }

    function toast(text) {
      if (api.env && typeof api.env.toast === "function") api.env.toast(text);
    }

    function isActive() {
      return api.state === STATE_RECORDING || api.state === STATE_PICKING || api.state === STATE_STOPPING;
    }

    function clearHeartbeat() {
      if (api.heartbeatTimer) {
        clearInterval(api.heartbeatTimer);
        api.heartbeatTimer = 0;
      }
    }

    function clearSubsPulse() {
      if (api.subsPulseTimer) {
        clearInterval(api.subsPulseTimer);
        api.subsPulseTimer = 0;
      }
    }

    function pulseMixSubscriptions() {
      if (!api.env || typeof api.env.ensureMixSubscriptions !== "function") return;
      try {
        api.env.ensureMixSubscriptions();
      } catch (_) {
        /* ignore */
      }
    }

    function startSubsPulse() {
      clearSubsPulse();
      pulseMixSubscriptions();
      // Раз в 2 с, не на каждом кадре: иначе setVideoQuality/setSubscribed душат UI и «Завершить» не кликается.
      api.subsPulseTimer = setInterval(() => {
        if (api.state === STATE_RECORDING) pulseMixSubscriptions();
      }, 2000);
    }

    function publishPresence(on) {
      if (!api.env || typeof api.env.publishPresence !== "function") return;
      try {
        api.env.publishPresence({ type: PRESENCE_TYPE, on: Boolean(on), at: Date.now() });
      } catch (_) {
        /* ignore */
      }
    }

    function startHeartbeat() {
      clearHeartbeat();
      publishPresence(true);
      api.heartbeatTimer = setInterval(() => publishPresence(true), HEARTBEAT_MS);
    }

    function detachVisibility() {
      if (!api.visibilityHandler) return;
      try {
        document.removeEventListener("visibilitychange", api.visibilityHandler);
      } catch (_) {
        /* ignore */
      }
      api.visibilityHandler = null;
    }

    function attachVisibility() {
      detachVisibility();
      api.visibilityHandler = () => {
        if (api.state !== STATE_RECORDING) return;
        if (typeof document !== "undefined" && document.hidden) {
          toast("Вкладка в фоне: запись может идти рывками. Держите окно созвона открытым.");
        }
      };
      try {
        document.addEventListener("visibilitychange", api.visibilityHandler);
      } catch (_) {
        /* ignore */
      }
    }

    function stopMix() {
      api.mixSources.forEach((src) => {
        try {
          src.disconnect();
        } catch (_) {
          /* ignore */
        }
      });
      api.mixSources = [];
      api.trackIds = new Set();
      if (api.audioCtx) {
        try {
          api.audioCtx.close();
        } catch (_) {
          /* ignore */
        }
      }
      api.audioCtx = null;
      api.mixDest = null;
    }

    function ensureMixGraph() {
      const Ctor = root.AudioContext || root.webkitAudioContext;
      if (!Ctor) throw new Error("AudioContext недоступен");
      if (!api.audioCtx) {
        api.audioCtx = new Ctor();
        api.mixDest = api.audioCtx.createMediaStreamDestination();
      }
      if (api.audioCtx.state === "suspended") {
        Promise.resolve(api.audioCtx.resume()).catch(() => undefined);
      }
      return api.mixDest.stream;
    }

    function syncMixTracks() {
      if (!api.env || typeof api.env.getMixAudioTracks !== "function" || !api.audioCtx || !api.mixDest) return;
      let tracks = [];
      try {
        tracks = api.env.getMixAudioTracks() || [];
      } catch (_) {
        tracks = [];
      }
      tracks.forEach((track) => {
        if (!track || track.readyState === "ended") return;
        const id = track.id || track.label || String(track);
        if (api.trackIds.has(id)) return;
        try {
          const stream = new MediaStream([track]);
          const src = api.audioCtx.createMediaStreamSource(stream);
          src.connect(api.mixDest);
          api.mixSources.push(src);
          api.trackIds.add(id);
        } catch (_) {
          /* ignore one track */
        }
      });
    }

    function stopCanvas() {
      if (api.raf) {
        cancelAnimationFrame(api.raf);
        api.raf = 0;
      }
      if (api.canvasStream) {
        api.canvasStream.getTracks().forEach((t) => {
          try {
            t.stop();
          } catch (_) {
            /* ignore */
          }
        });
      }
      api.canvasStream = null;
      api.canvas = null;
      api.ctx = null;
      api.lastFrameAt = 0;
    }

    function pausePaint() {
      // На время диалога «Завершить»: иначе paintTiles 1080p забивает main thread и confirm не кликается.
      api.paintPaused = true;
      if (api.raf) {
        cancelAnimationFrame(api.raf);
        api.raf = 0;
      }
    }

    function resumePaint() {
      if (!api.paintPaused) return;
      api.paintPaused = false;
      if (api.state === STATE_RECORDING && !api.raf) {
        api.raf = requestAnimationFrame(frameLoop);
      }
    }

    function frameLoop(now) {
      if (api.paintPaused || api.state !== STATE_RECORDING) return;
      const minGap = 1000 / TARGET_FPS;
      if (!api.lastFrameAt || now - api.lastFrameAt >= minGap - 1) {
        api.lastFrameAt = now;
        if (api.ctx && api.env && api.env.tilesEl) {
          try {
            paintTiles(api.ctx, api.env.tilesEl, CANVAS_W, CANVAS_H);
          } catch (_) {
            /* ignore frame */
          }
        }
        syncMixTracks();
      }
      if (!api.paintPaused) api.raf = requestAnimationFrame(frameLoop);
    }

    // Раньше Chrome showSaveFilePicker: файл «сохраняли» в проводнике, запись шла,
    // а в выбранной папке файла не оказывалось (handle/writable). Как в Firefox —
    // только скачивание через браузер в папку загрузок.
    async function pickSaveHandle() {
      return null;
    }

    // MediaRecorder даёт контейнер без индексов — бегунок в плеере мёртвый.
    // Remux на сервере через ffmpeg (+faststart) делает seekable MP4.
    function looksLikeMp4(bytes) {
      if (!bytes || bytes.length < 12) return false;
      // ISO BMFF: size(4) + 'ftyp' в первых 64 байтах.
      const n = Math.min(bytes.length, 64);
      for (let i = 0; i + 4 <= n; i++) {
        if (
          bytes[i] === 0x66 &&
          bytes[i + 1] === 0x74 &&
          bytes[i + 2] === 0x79 &&
          bytes[i + 3] === 0x70
        ) {
          return true;
        }
      }
      return false;
    }

    async function remuxViaServer(blob) {
      const remuxUrl = api.env && api.env.remuxUrl;
      if (!remuxUrl || !blob || !blob.size) return null;
      const fd = new FormData();
      // Имя входа — по сырому mime; выход всегда H.264+AAC MP4 (не copy Opus).
      const rawName = buildFileName(api.env && api.env.meetingId, new Date(), api.mime || "video/webm");
      fd.append("file", blob, rawName);
      const headers = {};
      try {
        if (window.PrimaryAnalitics && typeof PrimaryAnalitics.token === "function") {
          const token = PrimaryAnalitics.token();
          if (token) headers.Authorization = "Bearer " + token;
        }
      } catch (_) {
        /* ignore */
      }
      const ctrl = typeof AbortController === "function" ? new AbortController() : null;
      // Сервер REMUX_TIMEOUT_SEC=300; клиент чуть короче, чтобы не зависать навечно.
      const timer = ctrl ? setTimeout(() => ctrl.abort(), 280_000) : 0;
      try {
        const res = await fetch(remuxUrl, {
          method: "POST",
          body: fd,
          headers: headers,
          credentials: "same-origin",
          signal: ctrl ? ctrl.signal : undefined,
        });
        if (!res.ok) {
          toast("Remux не удался (HTTP " + res.status + ") — сохраняю без перемотки.");
          return null;
        }
        const buf = await res.arrayBuffer();
        if (!buf || buf.byteLength < 32) return null;
        const head = new Uint8Array(buf, 0, Math.min(64, buf.byteLength));
        if (!looksLikeMp4(head)) {
          toast("Remux вернул не-MP4 — сохраняю сырой файл.");
          return null;
        }
        return new Blob([buf], { type: "video/mp4" });
      } catch (err) {
        const aborted = err && err.name === "AbortError";
        toast(aborted ? "Remux прерван — сохраняю сырой файл." : "Remux недоступен — сохраняю сырой файл.");
        return null;
      } finally {
        if (timer) clearTimeout(timer);
      }
    }

    async function finalizeFile() {
      if (!api.chunks.length) {
        toast("Запись пуста — файл не создан.");
        return;
      }
      let blob = new Blob(api.chunks, { type: api.mime || "video/webm" });
      api.chunks = [];
      const durationMs = api.startedAt ? Date.now() - api.startedAt : 0;
      toast("Сохраняю запись (перемотка)…");
      let mimeForName = api.mime || "video/webm";
      let remuxed = null;
      try {
        remuxed = await remuxViaServer(blob);
      } catch (_) {
        remuxed = null;
      }
      if (remuxed) {
        blob = remuxed;
        mimeForName = "video/mp4";
      } else {
        // Fallback: патч Duration только для небольших WebM (иначе UI зависает на arrayBuffer).
        const isWebm = String(mimeForName).indexOf("webm") >= 0;
        if (isWebm && blob.size > 0 && blob.size < 80_000_000) {
          try {
            blob = await fixWebmDuration(blob, durationMs);
          } catch (_) {
            /* оставим как есть */
          }
        }
      }
      const name = buildFileName(api.env && api.env.meetingId, new Date(), mimeForName);
      api.fileHandle = null;
      triggerDownload(blob, name);
      toast(remuxed ? "Запись скачана (можно перематывать)." : "Запись скачана на этот компьютер.");
      await sealCrashSession(api);
    }

    async function start() {
      if (api.state !== STATE_IDLE && api.state !== STATE_ERROR) return;
      if (!api.env || !api.env.tilesEl) {
        toast("Комната ещё не готова к записи.");
        return;
      }
      if (!supportsLocalRecording()) {
        toast("Этот браузер не умеет локальную запись созвона.");
        return;
      }
      api.mime = pickMimeType();
      if (!api.mime) {
        toast("Нет подходящего формата записи.");
        return;
      }
      setState(STATE_PICKING);
      api.fileHandle = null;
      api.chunks = [];
      // AudioContext.resume() в Chrome надёжнее на жесте клика «Записать».
      try {
        ensureMixGraph();
      } catch (err) {
        setState(STATE_IDLE);
        toast("Не удалось открыть звук для записи: " + (err && err.message ? err.message : err));
        return;
      }

      try {
        if (api.env && typeof api.env.ensureMixSubscriptions === "function") {
          api.env.ensureMixSubscriptions();
        }
        api.canvas = document.createElement("canvas");
        api.canvas.width = CANVAS_W;
        api.canvas.height = CANVAS_H;
        api.ctx = api.canvas.getContext("2d", { alpha: false });
        paintTiles(api.ctx, api.env.tilesEl, CANVAS_W, CANVAS_H);
        api.canvasStream = api.canvas.captureStream(TARGET_FPS);
        const mixStream = ensureMixGraph();
        syncMixTracks();
        const out = new MediaStream([
          ...api.canvasStream.getVideoTracks(),
          ...mixStream.getAudioTracks(),
        ]);
        const created = createMediaRecorder(out, api.mime);
        api.mediaRecorder = created.recorder;
        if (created.mime) api.mime = created.mime;
        api.mediaRecorder.ondataavailable = (ev) => {
          if (!ev || !ev.data || !ev.data.size) return;
          api.chunks.push(ev.data);
          rememberChunk(api, ev.data);
        };
        api.mediaRecorder.onerror = () => {
          toast("Ошибка записи.");
          Promise.resolve(stop()).catch(() => undefined);
        };
        api.startedAt = Date.now();
        armCrashSession(api);
        api.mediaRecorder.start(TIMESLICE_MS);
        setState(STATE_RECORDING);
        attachVisibility();
        startHeartbeat();
        startSubsPulse();
        api.raf = requestAnimationFrame(frameLoop);
        toast("Идёт запись. После остановки файл скачается в папку загрузок браузера.");
      } catch (err) {
        await cleanupAfterFail();
        setState(STATE_ERROR);
        toast("Не удалось начать запись: " + (err && err.message ? err.message : err));
      }
    }

    async function cleanupAfterFail() {
      clearHeartbeat();
      clearSubsPulse();
      publishPresence(false);
      detachVisibility();
      stopCanvas();
      stopMix();
      if (api.mediaRecorder) {
        try {
          if (api.mediaRecorder.state !== "inactive") api.mediaRecorder.stop();
        } catch (_) {
          /* ignore */
        }
      }
      api.mediaRecorder = null;
      api.fileHandle = null;
      api.chunks = [];
      api.startedAt = 0;
      if (api.env && typeof api.env.releaseMixSubscriptions === "function") {
        try {
          api.env.releaseMixSubscriptions();
        } catch (_) {
          /* ignore */
        }
      }
      await sealCrashSession(api);
    }

    async function stop() {
      if (api.state !== STATE_RECORDING && api.state !== STATE_PICKING) return;
      setState(STATE_STOPPING);
      api.paintPaused = false;
      clearHeartbeat();
      clearSubsPulse();
      publishPresence(false);
      detachVisibility();
      stopCanvas();
      const recorder = api.mediaRecorder;
      api.mediaRecorder = null;
      if (recorder && recorder.state !== "inactive") {
        // Без таймаута stop иногда не приходит — leave/end зависают навсегда (лечится только reload).
        await new Promise((resolve) => {
          let settled = false;
          const done = () => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            resolve();
          };
          const timer = setTimeout(done, 2500);
          recorder.addEventListener("stop", done, { once: true });
          try {
            recorder.requestData();
          } catch (_) {
            /* ignore */
          }
          try {
            recorder.stop();
          } catch (_) {
            done();
          }
        });
      }
      stopMix();
      if (api.env && typeof api.env.releaseMixSubscriptions === "function") {
        try {
          api.env.releaseMixSubscriptions();
        } catch (_) {
          /* ignore */
        }
      }
      try {
        await finalizeFile();
      } catch (err) {
        toast("Не удалось сохранить файл: " + (err && err.message ? err.message : err));
      }
      api.chunks = [];
      api.startedAt = 0;
      setState(STATE_IDLE);
    }

    async function finalizePending() {
      if (!api.chunks.length) {
        api.startedAt = 0;
        setState(STATE_IDLE);
        return;
      }
      setState(STATE_STOPPING);
      try {
        await finalizeFile();
      } catch (err) {
        toast("Не удалось сохранить файл: " + (err && err.message ? err.message : err));
      }
      api.chunks = [];
      api.startedAt = 0;
      setState(STATE_IDLE);
    }

    async function dispose() {
      dropLiveRecorder(api);
      if (api.state === STATE_RECORDING || api.state === STATE_PICKING) {
        await stop();
      } else if (api.state === STATE_STOPPING && api.chunks.length) {
        await finalizePending();
      } else {
        await cleanupAfterFail();
        setState(STATE_IDLE);
      }
      api.env = null;
    }

    function attach(env) {
      api.env = env || null;
      if (api.env && api.env.recordBtn && supportsLocalRecording()) {
        api.env.recordBtn.hidden = false;
        api.env.recordBtn.disabled = false;
        api.env.recordBtn.title =
          "Записать созвон: после остановки файл скачается в папку загрузок браузера. В проект не загружается.";
      } else if (api.env && api.env.recordBtn) {
        api.env.recordBtn.hidden = false;
        api.env.recordBtn.disabled = true;
        api.env.recordBtn.title = "Локальная запись в этом браузере недоступна.";
      }
      return api;
    }

    async function toggle() {
      if (api.state === STATE_RECORDING) await stop();
      else if (api.state === STATE_IDLE || api.state === STATE_ERROR) await start();
    }

    api.attach = attach;
    api.start = start;
    api.stop = stop;
    api.finalizePending = finalizePending;
    api.toggle = toggle;
    api.dispose = dispose;
    api.isActive = isActive;
    api.pausePaint = pausePaint;
    api.resumePaint = resumePaint;
    api.getState = () => api.state;
    liveRecorders.push(api);
    return api;
  }

  const exported = {
    STATE_IDLE,
    STATE_PICKING,
    STATE_RECORDING,
    STATE_STOPPING,
    STATE_ERROR,
    PRESENCE_TYPE,
    MIME_CANDIDATES,
    TARGET_FPS,
    CANVAS_W,
    CANVAS_H,
    pickMimeType,
    createMediaRecorder,
    fileExt,
    buildFileName,
    parsePresencePayload,
    supportsLocalRecording,
    paintTiles,
    drawCover,
    drawContain,
    drawSpeakingRing,
    drawAvatar,
    tilePortraitRadius,
    fixWebmDuration,
    patchExistingDuration,
    insertDurationIntoInfo,
    createRecorder,
    recoverCrashRecording,
  };

  root.PrimaryAnaliticsLocalRecord = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  if (root.document) bootCrashRecovery();
})(typeof window !== "undefined" ? window : globalThis);
