/* Переход по ссылке к месту источника на странице отчёта (docs/plans/SOURCE-ANCHORS-PLAN.md, этап 4).
   ?t=754   — созвон: реплика с этого момента подсвечивается и прокручивается, плеер перематывается без звука (Я4);
   ?m=106   — переписка: сообщение с якорем #m106;
   ?q=текст — документ: первое совпадение цитаты (без учёта регистра и пробелов) подсвечивается.
   Подмешивается при отдаче отчёта (pages._serve_report) — сохранённые отчёты не пересобираются. */
(function () {
  const params = new URLSearchParams(location.search);
  const t = params.get('t');
  const m = params.get('m');
  const q = params.get('q');
  if (t === null && !m && !q) return;

  const style = document.createElement('style');
  style.textContent = '.jump-hit{outline:3px solid #f59e0b;outline-offset:2px;background:#fef3c7 !important;border-radius:6px}'
    + 'mark.jump-mark{background:#fde68a;padding:0 1px;border-radius:3px}'
    + '.jump-play{position:fixed;right:16px;bottom:16px;z-index:9999;border:0;border-radius:999px;padding:10px 16px;'
    + 'background:#5b7fa6;color:#fff;font:inherit;font-weight:600;box-shadow:0 4px 14px rgba(15,23,42,.25);cursor:pointer}';
  document.head.appendChild(style);

  const clock = (sec) => {
    const s = Math.max(0, Math.floor(sec));
    const h = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
    const two = (n) => String(n).padStart(2, '0');
    return h ? `${h}:${two(mm)}:${two(ss)}` : `${two(mm)}:${two(ss)}`;
  };
  function waitFor(check, done, tries = 80) {
    const found = check();
    if (found) { done(found); return; }
    if (tries > 0) setTimeout(() => waitFor(check, done, tries - 1), 150);
  }
  function hit(el) {
    el.classList.add('jump-hit');
    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  // Созвон: реплики рисует скрипт отчёта из JSON #data (data.turns[i].start) в элементы .turn в том же порядке.
  if (t !== null && Number.isFinite(Number(t))) {
    const sec = Number(t);
    let turns = [];
    try { turns = (JSON.parse(document.getElementById('data').textContent).turns || []); } catch (e) { turns = []; }
    waitFor(() => {
      const els = document.querySelectorAll('.turn');
      return els.length && (!turns.length || els.length >= turns.length) ? els : null;
    }, (els) => {
      let idx = 0;
      turns.forEach((turn, i) => { if (Number(turn.start) <= sec + 0.5) idx = i; });
      const el = els[Math.min(idx, els.length - 1)];
      el.classList.remove('hidden');
      hit(el);
      const player = document.getElementById('player');
      if (!player) return;
      const seek = () => { try { player.currentTime = sec; } catch (e) { /* ещё не перематывается */ } };
      if (player.readyState >= 1) seek(); else player.addEventListener('loadedmetadata', seek, { once: true });
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'jump-play';
      btn.textContent = `▶ Воспроизвести с ${clock(sec)}`;
      btn.addEventListener('click', () => { seek(); const p = player.play(); if (p && p.catch) p.catch(() => {}); btn.remove(); });
      document.body.appendChild(btn);
    });
    return;
  }

  function openSource() {
    const src = document.getElementById('doc-source');
    if (src && src.tagName === 'DETAILS') src.open = true;
  }

  // Документ: найти цитату в тексте без учёта регистра и пробелов, при неудаче — всё более короткое начало цитаты.
  function markQuote(needle) {
    const root = document.getElementById('doc-source') || document.querySelector('main') || document.body;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const map = [];
    let flat = '';
    let space = false;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.nodeValue;
      for (let i = 0; i < text.length; i += 1) {
        const ch = text[i];
        if (/\s/.test(ch)) { if (!space && flat) { flat += ' '; map.push([node, i]); } space = true; continue; }
        space = false;
        flat += ch.toLowerCase();
        map.push([node, i]);
      }
    }
    const base = needle.replace(/\s+/g, ' ').trim().toLowerCase();
    for (const len of [base.length, 60, 40, 25]) {
      const probe = base.slice(0, len).trim();
      if (probe.length < 8) continue;
      const at = flat.indexOf(probe);
      if (at < 0) continue;
      const [startNode, startOff] = map[at];
      const [endNode, endOff] = map[at + probe.length - 1];
      const range = document.createRange();
      range.setStart(startNode, startOff);
      range.setEnd(endNode, endOff + 1);
      const mark = document.createElement('mark');
      mark.className = 'jump-mark';
      mark.appendChild(range.extractContents());
      range.insertNode(mark);
      hit(mark);
      return true;
    }
    return false;
  }

  // Отчёт документа статичный (скрипт подключён с defer — разметка уже есть): якорь сообщения, иначе поиск цитаты.
  openSource();
  const anchor = m ? document.getElementById('m' + m) : null;
  if (anchor) { hit(anchor); return; }
  if (q) markQuote(q);
})();
