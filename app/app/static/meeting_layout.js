// Раскладка плиток встречи (SKKKKLayout): порядок экранов и камер, флаги микрофона/камеры, план звука.
(function (root) {
  const FLAG_NONE = "none";
  const FLAG_OFF = "off";
  const FLAG_SILENT = "silent";
  const FLAG_ON = "on";
  const KIND_SCREEN = "screen";
  const KIND_PERSON = "person";
  const SCREEN_ID_PREFIX = "meeting-tile-screen-";
  const ATTR_MIC_SILENT = "mic_silent";
  const ATTR_MIC_SILENT_ON = "1";
  const QUALITY_POOR = "poor";
  const QUALITY_LOST = "lost";
  const TEXT_QUALITY_POOR = "плохая связь";
  const TEXT_QUALITY_LOST = "нет связи";

  function flagState(pub, attrs) {
    if (!pub) return FLAG_NONE;
    if (pub.isMuted) return FLAG_OFF;
    if (attrs && attrs[ATTR_MIC_SILENT] === ATTR_MIC_SILENT_ON) return FLAG_SILENT;
    return FLAG_ON;
  }

  function soundPlan(soundOn, pubs) {
    return (pubs || []).map(function (pub) {
      return { pub: pub, subscribed: Boolean(soundOn) };
    });
  }

  function qualityKey(quality) {
    if (quality && typeof quality === "object") {
      return String(quality.quality || quality.value || "").toLowerCase();
    }
    return String(quality || "").toLowerCase();
  }

  function qualityBadge(quality) {
    const key = qualityKey(quality);
    if (key === QUALITY_POOR) return TEXT_QUALITY_POOR;
    if (key === QUALITY_LOST) return TEXT_QUALITY_LOST;
    return "";
  }

  function tileOrder(people, screens, publishedAt) {
    const stamps = publishedAt || {};
    const screenRows = (screens || []).slice().sort(function (left, right) {
      return Number(stamps[right.sid] || 0) - Number(stamps[left.sid] || 0);
    });
    const out = [];
    screenRows.forEach(function (screen, index) {
      out.push({
        kind: KIND_SCREEN,
        id: SCREEN_ID_PREFIX + String(screen.sid || ""),
        personId: screen.personId,
        lead: index === 0,
      });
    });
    (people || []).forEach(function (person) {
      out.push({ kind: KIND_PERSON, id: person.id });
    });
    return out;
  }

  // Подбор сетки плиток (пункт 3.11): максимум площади плитки заданного соотношения сторон,
  // чтобы вся сетка помещалась и по ширине, и по высоте сцены.
  function bestGrid(count, width, height, gap, ratio) {
    const n = Math.max(1, Math.floor(count) || 1);
    const r = ratio || 16 / 9;
    const g = gap || 0;
    const W = Math.max(1, width);
    const H = Math.max(1, height);
    let best = null;
    for (let cols = 1; cols <= n; cols += 1) {
      const rows = Math.ceil(n / cols);
      let w = (W - g * (cols - 1)) / cols;
      let h = w / r;
      const totalH = h * rows + g * (rows - 1);
      if (totalH > H) {
        h = (H - g * (rows - 1)) / rows;
        w = h * r;
      }
      if (w <= 0 || h <= 0) continue;
      const area = w * h;
      if (!best || area > best.area + 1e-6) {
        best = { cols: cols, rows: rows, tileW: Math.floor(w), tileH: Math.floor(h), area: area };
      }
    }
    return best || { cols: 1, rows: n, tileW: 0, tileH: 0, area: 0 };
  }

  root.SKKKKLayout = {
    bestGrid: bestGrid,
    flagState: flagState,
    soundPlan: soundPlan,
    qualityBadge: qualityBadge,
    tileOrder: tileOrder,
    FLAG_NONE: FLAG_NONE,
    FLAG_OFF: FLAG_OFF,
    FLAG_SILENT: FLAG_SILENT,
    FLAG_ON: FLAG_ON,
    KIND_SCREEN: KIND_SCREEN,
    KIND_PERSON: KIND_PERSON,
    SCREEN_ID_PREFIX: SCREEN_ID_PREFIX,
    ATTR_MIC_SILENT: ATTR_MIC_SILENT,
    ATTR_MIC_SILENT_ON: ATTR_MIC_SILENT_ON,
  };
})(typeof window !== "undefined" ? window : globalThis);
