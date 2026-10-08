/* Фоновая работа по проекту (ПФ9): ход разбора, перепроверка, переизвлечение, повтор ошибок.
   Смотрят админ и менеджер, запускает и останавливает только админ — сервер проверяет это сам. */
(function () {
  const root = document.getElementById('bg-card');
  if (!root || !window.PX) return;
  const PID = window.PX.projectId;
  const $ = (id) => document.getElementById(id);
  const esc = (t) => String(t ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const say = (text, bad) => { const el = $('bg-status'); el.textContent = text || ''; el.classList.toggle('text-danger', !!bad); };
  const errText = async (r) => { try { const d = (await r.json()).detail; return typeof d === 'string' ? d : JSON.stringify(d); } catch (e) { return 'Ошибка ' + r.status; } };
  const fmtTime = (iso) => { try { return new Date(iso).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }); } catch (e) { return ''; } };
  let timer = null;
  let lastMaterialsSig = '';

  async function api(path, method) {
    try {
      const r = await SKKKK.api(`/projects/${PID}/background${path}`, { method: method || 'GET', headers: method === 'POST' ? { 'Content-Type': 'application/json' } : undefined,
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
    badge.textContent = d.active ? `Идёт разбор: ${d.pending}` : (d.errors_total ? `Ошибок: ${d.errors_total}` : '');
    root.querySelector('.px-toggle-hint').textContent = d.active ? `(идёт, осталось ${d.pending})` : '(развернуть)';
    $('bg-idle').classList.toggle('d-none', d.active);
    $('bg-active').classList.toggle('d-none', !d.active);
    if (d.active) {
      const bar = $('bg-progress');
      const pct = d.percent == null ? 0 : d.percent;
      bar.style.width = pct + '%'; bar.textContent = pct + '%';
      $('bg-progress-wrap').setAttribute('aria-valuenow', String(pct));
      $('bg-current').innerHTML = d.current
        ? `Сейчас: <strong>${esc(d.current.stage)}</strong>${d.current.material ? ' — ' + esc(d.current.material) : ''} <span class="text-muted">с ${esc(fmtTime(d.current.since))}</span>`
        : 'Ждёт своей очереди (воркер занят расшифровкой или другой задачей).';
      $('bg-estimate').textContent = `Осталось задач: ${d.pending}` + (d.estimate_minutes ? `, примерно ${d.estimate_minutes} мин.` : '.');
    }
    $('bg-stages').innerHTML = d.stages.map((s) => {
      const left = s.queued + s.running;
      const cls = s.error ? 'text-bg-danger' : (left ? 'text-bg-info' : 'text-bg-light border');
      return `<span class="badge ${cls}" title="Выполнено ${s.done}, в очереди ${s.queued}, идёт ${s.running}, с ошибкой ${s.error}">${esc(s.label)}: ${s.done}${left ? ' / ' + (s.done + left) : ''}${s.error ? ' · ошибок ' + s.error : ''}</span>`;
    }).join('');
    $('bg-errors-box').classList.toggle('d-none', !d.errors_total);
    $('bg-errors-title').textContent = d.errors_total ? `Не удалось выполнить (за последние сутки): ${d.errors_total}` : '';
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
  load();
})();
