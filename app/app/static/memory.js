/* Память решений проекта: список предметов, история решения, состояние на дату, поправка итога человеком.
   Смотрят админ и менеджер проекта (менеджер — только в режиме live), режим и «Вернуть модели» — по правам сервера. */
(function () {
  const root = document.getElementById('mem-card');
  if (!root || !window.PX) return;
  const PID = window.PX.projectId;
  const $ = (id) => document.getElementById(id);
  const esc = (t) => String(t ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const PAGE = 30;
  const STATE_CLASS = { agreed: 'text-bg-success', implemented: 'text-bg-success', rejected: 'text-bg-danger', withdrawn: 'text-bg-secondary',
    superseded: 'text-bg-secondary', proposed: 'text-bg-warning', documented: 'text-bg-info', open: 'text-bg-light border' };
  const POS_CLASS = { current: 'text-bg-success', rejected: 'text-bg-danger', superseded: 'text-bg-secondary', open: 'text-bg-light border' };
  const SIDE = { customer: 'заказчик', executor: 'исполнитель' };
  const MODE_TEXT = { off: 'Память решений выключена: строится вручную, чат и менеджеры её не видят.',
    shadow: 'Режим наблюдения: память строится и видна только вам, чат её пока не использует. Когда проверки готовности пройдены, проект включается сам.',
    live: 'Память включена: её видят менеджеры, чат берёт из неё журнал решений.' };
  const DECIDE = [['agreed', 'Согласовано'], ['rejected', 'Отклонено'], ['implemented', 'Выполнено'], ['withdrawn', 'Снято'], ['superseded', 'Заменено'], ['reopened', 'Возобновить']];
  let offset = 0;
  let total = 0;
  let isAdmin = false;
  let timer = null;
  let watch = null;  // опрос сводки, пока память строится
  let wasBuilding = false;

  const say = (text, bad) => { const el = $('mem-status'); el.textContent = text || ''; el.classList.toggle('text-danger', !!bad); };
  const errText = async (r) => { try { const d = (await r.json()).detail; return typeof d === 'string' ? d : JSON.stringify(d); } catch (e) { return 'Ошибка ' + r.status; } };
  const fmtWhen = (iso) => { if (!iso) return ''; const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/); return m ? `${m[3]}.${m[2]}.${m[1]}${m[4] ? ' ' + m[4] + ':' + m[5] : ''}` : ''; };
  const fmtState = (s, labels) => (labels && labels[s]) || s;

  async function api(path, method, body) {
    try {
      const r = await PrimaryAnalitics.api(`/projects/${PID}/memory${path}`, { method: method || 'GET', headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined });
      if (r.status === 404 && path === '/summary') return { hidden: true };
      if (!r.ok) { say(await errText(r), true); return null; }
      return await r.json();
    } catch (e) { say('Нет связи с сервером. Проверьте интернет и повторите.', true); return null; }
  }

  function renderCounts(counts, labels) {
    const order = ['agreed', 'documented', 'proposed', 'open', 'rejected', 'superseded', 'implemented', 'withdrawn'];
    $('mem-counts').innerHTML = order.filter((k) => counts[k]).map((k) =>
      `<span class="badge ${STATE_CLASS[k] || 'text-bg-light border'}">${esc(fmtState(k, labels))}: ${counts[k]}</span>`).join('');
    const sel = $('mem-state');
    if (sel.options.length <= 1) {
      for (const k of order) { const o = document.createElement('option'); o.value = k; o.textContent = fmtState(k, labels); sel.appendChild(o); }
    }
  }

  async function loadSummary() {
    const d = await api('/summary');
    if (!d) return false;
    // память — вкладка блока «О проекте» (partials/project_hub.html): скрытую для проекта прячем вместе с кнопкой вкладки
    if (d.hidden) { if (window.PageHubs) PageHubs.setTabVisible('memory', false); return false; }
    if (window.PageHubs) PageHubs.setTabVisible('memory', true);
    isAdmin = !!d.is_admin;
    const total = Object.values(d.counts || {}).reduce((a, b) => a + b, 0);
    $('mem-mode').innerHTML = esc(MODE_TEXT[d.mode] || '') + (isAdmin ? ` <span class="ms-1">${['off', 'shadow', 'live'].filter((m) => m !== d.mode).map((m) =>
      `<button type="button" class="btn btn-sm btn-link p-0 me-2 mem-mode-set" data-mode="${m}">${m === 'off' ? 'Выключить' : (m === 'shadow' ? 'В наблюдение' : 'Включить сейчас')}</button>`).join('')}</span>` : '');
    const badge = $('mem-badge');
    badge.classList.toggle('d-none', !d.needs_attention);
    badge.textContent = d.needs_attention ? `Зависло: ${d.needs_attention}` : '';
    badge.title = 'Предметы, по которым предложили, но давно нет ответа';
    const tabCount = $('mem-tabcount');
    tabCount.textContent = total; tabCount.classList.toggle('d-none', !total);
    renderCounts(d.counts || {}, d.labels || {});
    const building = d.building || 0;
    $('mem-empty').classList.toggle('d-none', total > 0 || building > 0);
    $('mem-build').classList.toggle('d-none', !isAdmin || building > 0);
    if (building) say(`Память строится: в очереди задач ${building}. Карточка обновится сама, когда построение закончится.`);
    else if (wasBuilding) say(total ? 'Память построена.' : 'Построение закончено, но предметов не найдено.');
    if (building && !watch) watch = setInterval(poll, 15000);
    if (!building && watch) { clearInterval(watch); watch = null; }
    wasBuilding = building > 0;
    $('mem-filters').classList.toggle('d-none', !total);
    const box = $('mem-ready-box');
    const ready = d.readiness;
    box.classList.toggle('d-none', !(isAdmin && ready));
    if (ready) $('mem-ready').innerHTML = ready.checks.map((c) => `<li class="${c.ok ? 'text-success' : 'text-muted'}">${c.ok ? '✓' : '○'} ${esc(c.title)} <span class="text-muted">— ${esc(c.detail)}</span></li>`).join('')
      + `<li class="mt-1 fw-semibold">${ready.ready ? 'Проект готов к включению.' : 'Проект ещё не готов к включению.'}</li>`;
    return total > 0;
  }

  function row(it, labels) {
    const sub = [it.since ? 'с ' + fmtWhen(it.since) : '', it.acts ? `реплик: ${it.acts}` : '', it.human ? 'решено вручную' : '', it.positions ? `вариантов: ${it.positions}` : ''].filter(Boolean).join(' · ');
    return `<div class="border rounded p-2 mem-item" data-id="${it.id}">
      <button type="button" class="btn btn-link text-start text-decoration-none p-0 w-100 mem-open" aria-expanded="false">
        <span class="badge ${STATE_CLASS[it.state] || 'text-bg-light border'} me-2">${esc(it.state_label || fmtState(it.state, labels))}</span><strong>${esc(it.title)}</strong>
        <div class="small text-muted">${esc(sub)}</div>
        ${it.summary ? `<div class="small mt-1 text-body">${esc(it.summary)}</div>` : ''}
      </button>
      <div class="mem-detail mt-2 d-none"></div></div>`;
  }

  async function loadList() {
    const state = $('mem-state').value;
    const q = $('mem-q').value.trim();
    const date = $('mem-date').value;
    $('mem-asof').classList.add('d-none');
    if (date) return loadAsOf(date);
    const qs = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (state) qs.set('state', state);
    if (q) qs.set('q', q);
    const d = await api('/subjects?' + qs.toString());
    if (!d) return;
    total = d.total;
    $('mem-list').innerHTML = d.items.map((it) => row(it, d.labels)).join('') || '<div class="text-muted small">Ничего не найдено.</div>';
    const pages = Math.max(1, Math.ceil(total / PAGE));
    $('mem-pager').classList.toggle('d-none', pages <= 1);
    $('mem-page').textContent = `${Math.floor(offset / PAGE) + 1} из ${pages}`;
    $('mem-prev').disabled = offset === 0;
    $('mem-next').disabled = offset + PAGE >= total;
  }

  async function loadAsOf(date) {
    const d = await api('/state-at?date=' + encodeURIComponent(date));
    if (!d) return;
    const box = $('mem-asof');
    box.classList.remove('d-none');
    box.innerHTML = `<div class="small fw-semibold mb-1">Состояние на ${esc(fmtWhen(date))}: предметов ${d.items.length}</div>`;
    $('mem-pager').classList.add('d-none');
    $('mem-list').innerHTML = d.items.map((it) => `<div class="border rounded p-2"><span class="badge ${STATE_CLASS[it.state] || 'text-bg-light border'} me-2">${esc(it.state_label)}</span><strong>${esc(it.title)}</strong>
      ${it.since ? `<div class="small text-muted">с ${esc(fmtWhen(it.since))}${(it.flags || []).includes('ambiguous_order') ? ' · порядок реплик одного дня неизвестен' : ''}</div>` : ''}</div>`).join('')
      || '<div class="text-muted small">К этой дате решений ещё не было.</div>';
  }

  function detailHtml(d) {
    const acts = d.acts.map((a) => `<li class="mb-2"><div class="small text-muted">${esc(fmtWhen(a.at))}${a.side ? ' · ' + esc(SIDE[a.side] || a.side) : ''} · ${esc(a.author || '?')} — <strong>${esc(a.act_label)}</strong></div>
      <div>«${esc(a.quote)}»${(a.urls || []).map((u, i, all) => ` <a class="small" href="${esc(u)}" target="_blank" rel="noopener">открыть в материале${all.length > 1 ? ' ' + (i + 1) : ''}</a>`).join('')}</div></li>`).join('');
    const pos = d.positions.length ? `<div class="mb-2"><div class="small fw-semibold">Варианты и значения</div><ul class="small mb-0 ps-3">${d.positions.map((p) =>
      `<li><span class="badge ${POS_CLASS[p.state] || 'text-bg-light border'} me-1">${esc(p.state_label)}</span>${esc(p.label)}${p.replaced_by ? ` <span class="text-muted">→ ${esc(p.replaced_by)}</span>` : ''}</li>`).join('')}</ul></div>` : '';
    const events = d.events.length ? `<details class="mb-2"><summary class="small text-muted">Как менялось (${d.events.length})</summary><ul class="small mt-1 mb-0 ps-3">${d.events.map((e) =>
      `<li>${esc(fmtWhen(e.at))} — ${esc(e.kind_label)}${e.actor ? ', ' + esc(e.actor) : ''}${e.decided_by === 'human' ? ' <span class="badge text-bg-info">вручную</span>' : ''}${e.note ? ` <span class="text-muted">(${esc(e.note)})</span>` : ''}</li>`).join('')}</ul></details>` : '';
    const flag = (d.fold.flags || []).includes('ambiguous_order') ? '<div class="small text-muted mb-2">Часть реплик одного дня без времени: порядок между ними неизвестен.</div>' : '';
    const fix = `<div class="d-flex flex-wrap gap-1 align-items-center mem-fix"><span class="small text-muted me-1">Поправить итог:</span>${DECIDE.map(([k, t]) =>
      `<button type="button" class="btn btn-sm btn-outline-secondary mem-decide" data-kind="${k}">${t}</button>`).join('')}${d.subject.human ? '<button type="button" class="btn btn-sm btn-outline-primary mem-release">Вернуть модели</button>' : ''}</div>`;
    return `${pos}<ol class="ps-3 mb-2">${acts}</ol>${events}${flag}${fix}`;
  }

  async function openItem(el) {
    const box = el.querySelector('.mem-detail');
    const btn = el.querySelector('.mem-open');
    if (!box.classList.contains('d-none')) { box.classList.add('d-none'); btn.setAttribute('aria-expanded', 'false'); return; }
    box.innerHTML = '<div class="small text-muted">Загрузка…</div>';
    box.classList.remove('d-none');
    btn.setAttribute('aria-expanded', 'true');
    const d = await api('/subjects/' + el.dataset.id);
    if (!d) { box.classList.add('d-none'); return; }
    d.subject.human = d.events.some((e) => e.decided_by === 'human');
    box.innerHTML = detailHtml(d);
  }

  async function decide(el, kind) {
    const note = kind === 'reopened' ? '' : (window.prompt('Комментарий (необязательно):', '') || '');
    const d = await api(`/subjects/${el.dataset.id}/decide`, 'POST', { kind, note });
    if (d) { say('Итог поправлен.'); await refreshAfterChange(el); }
  }

  async function refreshAfterChange(el) {
    const id = el.dataset.id;
    await loadList();
    await loadSummary();
    const fresh = root.querySelector(`.mem-item[data-id="${id}"]`);
    if (fresh) await openItem(fresh);
  }

  root.addEventListener('click', async (e) => {
    const t = e.target.closest('button');
    if (!t) return;
    const item = t.closest('.mem-item');
    if (t.classList.contains('mem-open') && item) return openItem(item);
    if (t.classList.contains('mem-decide') && item) return decide(item, t.dataset.kind);
    if (t.classList.contains('mem-release') && item) {
      const d = await api(`/subjects/${item.dataset.id}/release`, 'POST');
      if (d) { say('Предмет возвращён модели.'); await refreshAfterChange(item); }
      return;
    }
    if (t.id === 'mem-build') {
      const d = await api('/build', 'POST');
      if (d) { say(`Поставлено в очередь задач: ${d.jobs_queued}. Карточка обновится сама, когда память построится.`); await loadSummary(); }
      return;
    }
    if (t.classList.contains('mem-mode-set')) {
      const d = await api('/mode', 'PUT', { mode: t.dataset.mode });
      if (d) { say('Режим изменён.'); await loadSummary(); }
    }
  });
  const reload = () => { offset = 0; loadList(); };
  $('mem-state').addEventListener('change', reload);
  $('mem-date').addEventListener('change', reload);
  $('mem-q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(reload, 350); });
  $('mem-prev').addEventListener('click', () => { offset = Math.max(0, offset - PAGE); loadList(); });
  $('mem-next').addEventListener('click', () => { offset += PAGE; loadList(); });

  async function poll() {
    const building = wasBuilding;
    const has = await loadSummary();
    if (building && !wasBuilding && has) { offset = 0; await loadList(); loaded = true; }
  }

  let loaded = false;
  async function init() {
    const has = await loadSummary();
    if (has) { await loadList(); loaded = true; }
  }
  document.addEventListener('hub:show', (ev) => { if (ev.detail && ev.detail.tab === 'memory' && !loaded) init(); });
  init();
})();
