/* Полоска «Что нового с прошлого захода» (docs/plans/PORTAL-ACCURACY-MANAGER-PLAN.md, Ш5).
   Строки рисует сервер (pages.project_page → project_digest); здесь — переход к нужной карточке, «Скрыть», обновление после
   тоста «файл обработан» и строка «Ждут вас»: её счёт тяжёлый (контракт), поэтому он не задерживает открытие страницы.
   DISABLED: временно не показываем в UI (2026-11-05); API и разметка остаются. */
(function () {
  const DISABLED = true;
  const box = document.getElementById('project-digest');
  const list = document.getElementById('project-digest-list');
  if (DISABLED || !box || !list || !window.PX) {
    if (box) box.hidden = true;
    window.PXDigest = { async reload() {} };
    return;
  }
  const PID = window.PX.projectId;
  const staff = window.PX.role === 'admin' || window.PX.role === 'manager';
  const esc = (t) => String(t ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  // куда ведёт строка: карточка (её панель раскрывается) или страница
  const TARGETS = {
    materials: { card: '.workspace-card', panel: 'artifacts-panel' },
    tasks: { card: '.deadlines-card', panel: 'deadlines-panel' },
    memory: { card: '#project-hub-card', hub: 'memory' },
    exchange: { card: '#agents-hub-card', hub: 'exchange' },
    review: { card: '#project-hub-card', hub: 'review' },
    conflicts: { href: '/admin/conflicts' },
  };

  function open(target, view) {
    const t = TARGETS[target];
    if (!t) return;
    if (t.href) { window.location.href = t.href; return; }
    const card = document.querySelector(t.card);
    if (!card) return;
    if (t.hub && window.PageHubs) window.PageHubs.open(t.hub);
    const panel = t.panel && document.getElementById(t.panel);
    if (panel && !panel.classList.contains('show')) bootstrap.Collapse.getOrCreateInstance(panel).show();
    if (target === 'review' && view) {
      const sel = document.getElementById('px-rv-verification');
      if (sel && sel.value !== view) { sel.value = view; sel.dispatchEvent(new Event('change')); }
    }
    if (target === 'tasks' && view) {
      setTimeout(() => document.querySelector(`#tasks-board [data-view="${view}"]`)?.click(), 300);
    }
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function lineHtml(line) {
    return `<li><a href="#" data-digest-target="${esc(line.target)}" data-digest-view="${esc(line.view || '')}">${esc(line.text)}</a></li>`;
  }

  function render(d) {
    const lines = (d && d.lines) || [];
    list.innerHTML = lines.map(lineHtml).join('');
    box.hidden = !lines.length;
    addWaiting();
  }

  async function addWaiting() {
    if (!staff || box.dataset.waitingLoaded === '1') return;
    try {
      const r = await PrimaryAnalitics.api(`/projects/${PID}/agent-exchange/summary`);
      if (!r.ok) return;
      const s = await r.json();
      const n = s.requirements_unconfirmed || 0;
      if (!n || box.hidden) return;  // одна «Ждут вас» без нового не показывает полоску: она видна в карточке требований
      box.dataset.waitingLoaded = '1';
      list.insertAdjacentHTML('beforeend', lineHtml({ target: 'review', view: 'needs_human', text: `Ждут вашего решения требований: ${n}` }));
    } catch (e) { /* строка не критична */ }
  }

  list.addEventListener('click', (ev) => {
    const a = ev.target.closest('[data-digest-target]');
    if (!a) return;
    ev.preventDefault();
    open(a.dataset.digestTarget, a.dataset.digestView);
  });
  document.getElementById('project-digest-hide')?.addEventListener('click', async () => {
    box.hidden = true;
    try { await PrimaryAnalitics.api(`/projects/${PID}/digest/dismiss`, { method: 'POST' }); } catch (e) { /* скрыто на экране и так */ }
  });

  window.PXDigest = {
    async reload() {
      try {
        const r = await PrimaryAnalitics.api(`/projects/${PID}/digest`);
        if (r.ok) { box.dataset.waitingLoaded = ''; render(await r.json()); }
      } catch (e) { /* полоска не критична */ }
    },
  };
  addWaiting();
})();
