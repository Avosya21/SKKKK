/* Дальнейшая обработка проекта: строка хода текущей очереди, ошибки, перепроверка и переизвлечение.
   Живёт строкой-<details> в «Материалах проекта» под ходом расшифровки (partials/background_progress.html), значок — в шапке «Материалов».
   Смотрят админ и менеджер, запускает и останавливает только админ — сервер проверяет это сам. */
(function () {
  const root = document.getElementById('bg-card');
  if (!root || !window.PX) return;
  const PID = window.PX.projectId;
  const $ = (id) => document.getElementById(id);
  const esc = (t) => String(t ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const say = (text, bad) => { const el = $('bg-status'); el.textContent = text || ''; el.classList.toggle('text-danger', !!bad); };
  const errText = async (r) => { try { const d = (await r.json()).detail; return typeof d === 'string' ? d : JSON.stringify(d); } catch (e) { return 'Ошибка ' + r.status; } };
  let timer = null;
  let lastMaterialsSig = '';

  async function api(path, method) {
    try {
      const r = await PrimaryAnalitics.api(`/projects/${PID}/background${path}`, { method: method || 'GET', headers: method === 'POST' ? { 'Content-Type': 'application/json' } : undefined,
        body: method === 'POST' && path !== '/retry' && path !== '/cancel' ? JSON.stringify({ artifact_ids: selected() }) : (method === 'POST' ? '{}' : undefined) });
      if (!r.ok) { say(await errText(r), true); return null; }
      return await r.json();
    } catch (e) { say('Нет связи с сервером. Проверьте интернет и повторите.', true); return null; }
  }
  const selected = () => [...document.querySelectorAll('#bg-materials input:checked')].map((i) => Number(i.value));

  function render(d) {
    const badge = $('bg-badge');
    badge.classList.toggle('d-none', !d.active && !d.errors_total);
    badge.className = 'badge flex-shrink-0 ' + (d.active ? 'text-bg-info' : 'text-bg-danger') + (d.active || d.errors_total ? '' : ' d-none');
    badge.textContent = d.active ? `Идёт: ${d.pending}` : (d.errors_total ? `Ошибок: ${d.errors_total}` : '');
    const state = $('bg-summary');
    const fraction = `${d.done} из ${d.total}`;
    state.textContent = d.active
      ? (d.current ? `${d.current.stage}${d.current.material ? ' — ' + d.current.material : ''} · ${fraction}` : `В очереди: ${d.pending}`)
      : (d.errors_total ? `Не выполнено: ${d.errors_total}` : 'Сейчас ничего не идёт');
    state.classList.toggle('is-active', !!d.active);
    state.classList.toggle('is-error', !d.active && !!d.errors_total);
    $('bg-active').classList.toggle('d-none', !d.active);
    if (d.active) {
      const pct = d.percent == null ? 0 : d.percent;
      $('bg-progress').style.width = pct + '%';
      $('bg-progress-wrap').setAttribute('aria-valuenow', String(pct));
      $('bg-progress-wrap').setAttribute('aria-valuetext', fraction);
      $('bg-fraction').textContent = fraction;
      const waiting = $('bg-current');
      waiting.textContent = d.current ? '' : 'Ждёт очереди';
      waiting.classList.toggle('d-none', !!d.current);
    }
    const leftStages = d.stages.filter((s) => s.queued + s.running > 0);
    const showStages = leftStages.length > 1;
    const stages = $('bg-stages');
    stages.classList.toggle('d-none', !showStages);
    stages.innerHTML = leftStages.map((s) => {
      const left = s.queued + s.running;
      return `<span class="badge text-bg-light border" title="В очереди ${s.queued}, идёт ${s.running}">${esc(s.label)} · ${left}</span>`;
    }).join('');
    $('bg-errors-box').classList.toggle('d-none', !d.errors_total);
    $('bg-errors-title').textContent = d.errors_total ? `Не выполнено: ${d.errors_total}` : '';
    $('bg-errors').innerHTML = d.errors.map((e) => `<li>${esc(e.stage)} — ${esc(e.material)}${e.message ? `: <span class="text-muted">${esc(e.message)}</span>` : ''}`
      + (e.retry === false ? ' <span class="text-muted">(расшифровку повторяют из карточки материала)</span>' : '') + '</li>').join('');
    const admin = !!d.can_run;
    $('bg-actions').classList.toggle('d-none', !admin);
    $('bg-select-box').classList.toggle('d-none', !admin);
    $('bg-retry').classList.toggle('d-none', !(d.retryable ?? d.errors_total));
    $('bg-cancel').classList.toggle('d-none', !((d.cancellable ?? d.pending) > 0));
    for (const id of ['bg-review', 'bg-reextract']) $(id).disabled = !d.worker_ready;
    if (!d.worker_ready && admin) say('Воркер фоновых задач не запущен или старой версии: перезапустите сервер, иначе задачи не выполнятся.', true);
    const sig = d.materials.map((m) => m.id).join(',');
    if (sig !== lastMaterialsSig) {
      const keep = new Set(selected());
      $('bg-materials').innerHTML = d.materials.map((m) => `<label class="form-check small"><input class="form-check-input" type="checkbox" value="${m.id}"${keep.has(m.id) ? ' checked' : ''}> ${esc(m.name)} <span class="text-muted">(${esc(m.kind)})</span></label>`).join('')
        || '<div class="text-muted small">Готовых материалов нет.</div>';
      lastMaterialsSig = sig;
    }
  }

  async function load() {
    const d = await api('');
    if (!d) return;
    if (d.worker_ready || !d.can_run) say('');
    render(d);
    clearTimeout(timer);
    timer = setTimeout(load, d.active ? 5000 : 30000);  // пока идёт разбор — чаще
  }

  async function start(path, ask, done) {
    const n = selected().length;
    if (!confirm(ask + (n ? ` Только выбранные материалы: ${n}.` : ' Все материалы проекта.'))) return;
    const r = await api(path, 'POST');
    if (r) { say(done(r)); load(); }
  }
  $('bg-review').addEventListener('click', () => start('/review', 'Перепроверить: требования, темы, устаревшее и дубли, конфликты, выжимка. Идёт фоном, десятки минут, расшифровке не мешает.',
    (r) => `Поставлено задач: ${r.jobs_queued} (${r.mode === 'live' ? 'с применением' : 'в тени, без последствий'}).`));
  $('bg-reextract').addEventListener('click', () => start('/reextract', 'Заново извлечь требования. Ваши решения (подтверждения, отказы, статусы) переносятся на новые пункты.',
    (r) => `Поставлено в очередь материалов: ${r.jobs_queued} из ${r.materials}.`));
  $('bg-retry').addEventListener('click', async () => { const r = await api('/retry', 'POST'); if (r) { say(`Возвращено в очередь: ${r.requeued}.`); load(); } });
  $('bg-cancel').addEventListener('click', async () => {
    if (!confirm('Убрать из очереди все ждущие задачи этого проекта? Идущая сейчас доработает.')) return;
    const r = await api('/cancel', 'POST');
    if (r) { say(`Убрано из очереди: ${r.cancelled}.`); load(); }
  });
  // открытость строки помним, как у остальных сворачиваемых блоков
  try { if (localStorage.getItem('bg_inline_open') === '1') root.open = true; } catch (e) { /* без хранилища работает */ }
  root.addEventListener('toggle', () => { try { localStorage.setItem('bg_inline_open', root.open ? '1' : '0'); } catch (e) { /* без хранилища работает */ } });
  load();
})();
