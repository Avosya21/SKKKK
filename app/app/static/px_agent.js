/* Карточки обмена с агентами на странице проекта: подтверждение требований, обмен (вопросы/вердикты/журнал), настройки.
   Обмен и настройки — вкладки блока «Агенты и MCP» (partials/agents_hub.html), требования, вопросы к заказчику и карточка проекта — блока «О проекте»
   (partials/project_hub.html); переключает их static/tab_hubs.js.
   Данные и права приходят с сервера (can_edit); все действия проверяются на сервере. Роль admin — действия, manager — просмотр. */
(function () {
  if (!document.getElementById('project-hub-card') || !window.PX) return;
  const PID = window.PX.projectId;
  const $ = (id) => document.getElementById(id);
  const esc = (t) => String(t ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const errText = async (r) => {
    try {
      const d = (await r.json()).detail;
      const s = typeof d === 'string' ? d : JSON.stringify(d);
      return (window.PrimaryAnaliticsErrors && window.PrimaryAnaliticsErrors.text(s)) || s;
    } catch (e) { return 'Ошибка ' + r.status; }
  };
  // Потеря сети или падение сервера не должны молча ломать блок: возвращаем «ответ» с понятным текстом, который показывает errText
  const api = async (path, opts) => {
    try {
      return await PrimaryAnalitics.api(`/projects/${PID}${path}`, opts);
    } catch (e) {
      return new Response(JSON.stringify({ detail: 'Нет связи с сервером. Проверьте интернет и повторите.' }), { status: 599, headers: { 'Content-Type': 'application/json' } });
    }
  };
  const post = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  const fmt = (iso) => { try { return new Date(iso).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }); } catch (e) { return iso || ''; } };
  const say = (key, text, bad) => { const el = $('px-status-' + key); if (el) { el.textContent = text || ''; el.classList.toggle('text-danger', !!bad); } };
  const SIDE = { customer: 'заказчик', vendor: 'исполнитель', unknown: 'сторона неизвестна' };
  const STATUS = { agreed: 'согласовано', proposed: 'предложено', implemented: 'реализовано', rejected: 'отклонено', agreed_out_of_scope: 'согласовано вне ТЗ' };
  const BASIS = {
    customer_evidence: 'есть реплика заказчика', decision_by_vendor_only: 'только реплика исполнителя', decision_side_unknown: 'сторона реплики неизвестна',
    customer_conditional: 'заказчик с оговоркой', task_not_confirmed: 'задача, не подтверждена', human_set: 'задано человеком', task_cancelled: 'задача отменена',
  };
  const DECISION = { accepted: 'принято', rejected: 'отклонено', needs_human: 'ждёт человека' };
  const VERDICT = { implemented: 'реализовано', partially_implemented: 'частично', not_implemented: 'не реализовано', contradicts_data: 'противоречит данным', not_verifiable: 'нельзя проверить' };
  const loaded = {};

  function setCount(key, n, extra) {
    const el = $('px-count-' + key);
    if (!el) return;
    el.textContent = extra ? `${n} ${extra}` : String(n);
    el.classList.toggle('d-none', !n);
    el.classList.toggle('text-bg-warning', !!n);
    el.classList.toggle('text-bg-secondary', !n);
  }

  async function loadSummary() {
    try {
      const r = await api('/agent-exchange/summary');
      if (!r.ok) return;
      const s = await r.json();
      setCount('review', s.requirements_unconfirmed, 'ждут вас');
      setCount('exchange', s.questions_waiting + s.verdicts_waiting, 'ждут вас');
      const hubCount = $('agents-hub-tabcount-exchange'), waiting = s.questions_waiting + s.verdicts_waiting;
      if (hubCount) { hubCount.textContent = waiting; hubCount.classList.toggle('d-none', !waiting); }
      const q = $('px-tabcount-questions'), v = $('px-tabcount-verdicts');
      q.textContent = s.questions_waiting; q.classList.toggle('d-none', !s.questions_waiting);
      v.textContent = s.verdicts_waiting; v.classList.toggle('d-none', !s.verdicts_waiting);
    } catch (e) { /* счётчики не критичны */ }
  }

  /* ---------- ленивая загрузка вкладок ---------- */
  // вкладки блоков «Агенты и MCP» и «О проекте» (static/tab_hubs.js): данные грузятся, когда вкладка впервые видна
  document.addEventListener('hub:show', (ev) => {
    const key = ev.detail && ev.detail.tab;
    if (LOADERS[key] && !loaded[key]) { loaded[key] = true; LOADERS[key](); }
  });

  /* ---------- Подтверждение требований ---------- */
  const rv = { offset: 0, total: 0, canEdit: false, selected: new Set() };
  const RV_LIMIT = 30;

  const MODEL_VERDICT = { confirm: 'подтвердила', reject: 'отклонила', unsure: 'не уверена' };

  function modelHtml(r) {
    const m = r.model_review;
    if (!m || m.undone) return '';
    const live = m.mode === 'live';
    const what = live ? `Модель ${MODEL_VERDICT[m.verdict] || m.verdict}` : `Модель ${MODEL_VERDICT[m.verdict] || m.verdict} бы (тень)`;
    const conf = m.confidence != null ? ` · уверенность ${Math.round(m.confidence * 100)}%` : '';
    return `<div class="small px-model mt-1"><span class="text-muted">${esc(what)}${conf}:</span> ${esc(m.reason || '')}</div>`;
  }

  // Кто и когда по источнику требования (REQUIREMENTS-PLAN А9): дата загрузки не выдаётся за дату встречи.
  const dayRu = (iso) => { const [y, m, d] = String(iso).slice(0, 10).split('-'); return d && m && y ? `${d}.${m}.${y}` : String(iso); };
  const sideOf = (side) => (side && side !== 'unknown' ? ` (${esc(SIDE[side] || side)})` : '');
  function whoWhen(s) {
    const who = s.speaker ? `${esc(s.speaker)}${sideOf(s.side)}` : 'говорящий не опознан';
    const basis = s.speaker_basis === 'voice' ? ' <span class="badge text-bg-light border" title="Имя определено по голосу, а не по дорожке микрофона">по голосу</span>'
      : s.speaker_basis === 'words' ? ' <span class="badge text-bg-warning" title="Пункт не привязан к реплике: говорящий найден по совпадению слов и может быть неточен">по совпадению слов</span>' : '';
    const agreed = (s.agreed_by || []).length ? ` · утвердил: ${s.agreed_by.map((a) => esc(a.name) + sideOf(a.side)).join(', ')}` : '';
    const where = s.material ? ` · ${esc(s.material)}${s.paragraph ? ', абзац ' + esc(s.paragraph) : ''}` : '';
    let when;
    if (s.date_basis === 'message' && s.said_at) when = esc(fmt(s.said_at));
    else if (s.said_date && s.date_basis === 'upload') when = esc(dayRu(s.said_date)) + ' <span class="badge text-bg-warning" title="Дата записи неизвестна — взята дата загрузки; поставьте дату на странице проекта">по дате загрузки</span>' + (s.timecode ? `, ${esc(s.timecode)}` : '');
    else if (s.said_date) when = esc(dayRu(s.said_date)) + (s.timecode ? `, ${esc(s.timecode)}` : '');
    else if (s.said_at && !s.date_basis) when = esc(fmt(s.said_at));
    else when = '<span class="badge text-bg-warning" title="У материала нет даты записи — поставьте её на странице проекта">дата записи неизвестна</span>' + (s.timecode ? `, ${esc(s.timecode)}` : '');
    // Ссылка на место источника (SOURCE-ANCHORS-PLAN этап 4): созвон — на реплике, переписка — на сообщении
    const open = s.url ? ` · <a href="${esc(s.url)}" target="_blank" rel="noopener" title="Открыть источник на этом месте">${s.timecode ? '▶ ' + esc(s.timecode) : 'открыть ▸'}</a>` : '';
    return `Сказал: ${who}${basis}${agreed}${where} · ${when}${open}`;
  }

  // Вид пункта (А7), тема (А2), ход обсуждения, что заменило (А6) — REQUIREMENTS-PLAN этап 8.
  const ITEM_KIND = { requirement: 'требование к системе', project: 'решение по проекту', work: 'работа по системе' };
  function contextHtml(r) {
    if (!r.context || !r.context.length) return '';
    const lines = r.context.map((c) => `<li${c.source ? ' class="fw-semibold"' : ''}>${esc(c.timecode || '')} ${c.speaker === null && /^абзац/.test(c.timecode || '') ? '' : esc(c.speaker || 'говорящий не опознан') + ':'} ${esc(c.text)}</li>`).join('');
    return `<details class="small mt-1"><summary>${(r.context || []).some((c) => /^абзац/.test(c.timecode || '')) ? 'Текст вокруг' : 'Ход обсуждения'}</summary><ul class="mb-0 list-unstyled">${lines}</ul></details>`;
  }
  function replacedHtml(r) {
    if (r.hidden_state === 'superseded') {
      const b = r.replaced_by;
      const rel = r.relevance || {};
      const shadow = rel.mode === 'shadow' ? ' (так считает модель, решение ещё не применено)' : '';
      return `<div class="small mt-1 px-replaced"><span class="badge text-bg-secondary">устарело${shadow}</span> `
        + (b ? `Заменено: <b>${esc(b.statement)}</b><br><span class="text-muted">${whoWhen(b)}</span>` : '')
        + (rel.quote ? `<div class="text-muted">Основание: «${esc(rel.quote)}»</div>` : '') + '</div>';
    }
    if (r.hidden_state === 'duplicate') {
      const b = r.replaced_by;
      return `<div class="small mt-1"><span class="badge text-bg-secondary">дубль</span> ${b ? `Та же мысль: <b>${esc(b.statement)}</b>` : ''}</div>`;
    }
    const rel = r.relevance || {};
    return rel.state === 'done' && !rel.undone ? '<div class="small mt-1"><span class="badge text-bg-info">по более поздним материалам — выполнено</span></div>' : '';
  }

  function reqHtml(r) {
    const ok = r.verification === 'human_confirmed';
    const byModel = r.verification === 'model_confirmed' || r.verification === 'model_rejected';
    const undoable = byModel && r.model_review && !r.model_review.undone && r.source_entity_id;
    const sources = r.sources && r.sources.length ? r.sources : (r.quote ? [{ quote: r.quote, speaker: r.speaker, side: r.side, said_at: r.said_at }] : []);
    const first = sources[0];
    const more = sources.slice(1).map((s) => `<li>${whoWhen(s)}${s.quote ? ` — «${esc(s.quote)}»` : ''}</li>`).join('');
    const quote = first ? `<blockquote class="px-quote">«${esc(first.quote || '')}»<footer>${whoWhen(first)}</footer></blockquote>`
      + (more ? `<details class="small mt-1"><summary>Ещё звучало: ${sources.length - 1}</summary><ul class="mb-0">${more}</ul></details>` : '')
      : (r.verification === 'model_rejected' ? '' : '<div class="small text-muted">Цитаты в источнике не найдено.</div>');
    const hr = r.human_review ? `<div class="small text-muted mt-1">Решение человека: ${esc(r.human_review.by)}, ${esc(fmt(r.human_review.at))}${r.human_review.reason ? ' — ' + esc(r.human_review.reason) : ''}</div>` : '';
    const undo = rv.canEdit && undoable ? `<button type="button" class="btn btn-sm btn-outline-secondary" data-rv="undo" data-id="${esc(r.id)}" data-entity="${esc(r.source_entity_id)}">Вернуть решение модели</button>` : '';
    const spotActs = r.spot ? `<div class="px-actions">
        <button type="button" class="btn btn-sm btn-outline-success" data-rv="spot-ok" data-id="${esc(r.id)}" data-entity="${esc(r.source_entity_id)}" title="Пункт извлечён верно: так и было сказано или записано">Верно</button>
        <button type="button" class="btn btn-sm btn-outline-danger" data-rv="spot-wrong" data-id="${esc(r.id)}" data-entity="${esc(r.source_entity_id)}" title="Пункт извлечён неверно: такого не говорили или смысл искажён">Неверно</button></div>` : '';
    const acts = r.spot ? spotActs : rv.canEdit && r.verification === 'model_rejected' ? `<div class="px-actions">${undo}</div>` : (rv.canEdit ? `<div class="px-actions">
        ${ok ? '' : `<button type="button" class="btn btn-sm btn-primary" data-rv="confirm" data-id="${esc(r.id)}" title="Подтверждаю: это действительно было сказано / решено на этой встрече">Подтвердить</button>`}
        ${r.hidden_state && r.source_entity_id ? `<button type="button" class="btn btn-sm btn-outline-success" data-rv="current" data-entity="${esc(r.source_entity_id)}" title="Модель ошиблась: пункт по-прежнему актуален">Это актуально</button>` : ''}
        <button type="button" class="btn btn-sm btn-outline-secondary" data-rv="status" data-id="${esc(r.id)}">Изменить статус</button>
        <button type="button" class="btn btn-sm btn-outline-danger" data-rv="reject" data-id="${esc(r.id)}">Отклонить</button>${undo}</div>` : '');
    return `<div class="px-item" data-item="${esc(r.id)}">
      <div class="d-flex gap-2 align-items-start">
        ${rv.canEdit && !ok && !r.spot && r.verification !== 'model_rejected' ? `<input type="checkbox" class="form-check-input mt-1 px-rv-check" data-id="${esc(r.id)}" aria-label="Выбрать ${esc(r.id)}">` : ''}
        <div class="flex-grow-1">
          <div class="d-flex flex-wrap gap-2 align-items-center mb-1">
            <code class="small">${esc(r.id)}</code>
            <span class="badge ${r.status === 'agreed' ? 'text-bg-success' : 'text-bg-light border'}">${esc(STATUS[r.status] || r.status)}</span>
            <span class="small text-muted">${esc(BASIS[r.status_basis] || r.status_basis || '')}</span>
            ${ok ? '<span class="badge text-bg-primary">подтверждено человеком</span>'
              : r.verification === 'model_confirmed' ? '<span class="badge text-bg-success">подтверждено моделью</span>'
              : r.verification === 'model_rejected' ? '<span class="badge text-bg-secondary">отклонено моделью</span>'
              : '<span class="badge text-bg-warning">не подтверждено</span>'}
            ${r.model_review && r.model_review.audit && !ok ? '<span class="badge text-bg-info" title="Контрольная выборка: проверьте решение модели">проверка модели</span>' : ''}
            ${r.analysis ? `<span class="badge text-bg-info" title="Вердикт агента анализа">агент: ${esc(VERDICT[r.analysis.verdict] || r.analysis.verdict)}</span>` : ''}
            ${r.item_kind && ITEM_KIND[r.item_kind] ? `<span class="badge text-bg-light border">${esc(ITEM_KIND[r.item_kind])}</span>` : ''}
            ${r.unclear_wording ? '<span class="badge text-bg-warning" title="В формулировке не назван объект — уточните при подтверждении">нечёткая формулировка</span>' : ''}
          </div>
          <div class="px-statement">${esc(r.statement)}</div>
          ${quote}${contextHtml(r)}${replacedHtml(r)}${modelHtml(r)}${hr}${acts}
          <div class="px-inline d-none" data-inline="${esc(r.id)}"></div>
        </div>
      </div></div>`;
  }

  async function loadReview(append) {
    const box = $('px-rv-list');
    if (!append) { rv.offset = 0; rv.selected.clear(); box.innerHTML = ''; }
    const p = new URLSearchParams({ offset: rv.offset, limit: RV_LIMIT });
    for (const [k, id] of [['verification', 'px-rv-verification'], ['status', 'px-rv-status'], ['side', 'px-rv-side'], ['q', 'px-rv-q']]) { const v = $(id).value.trim(); if (v) p.set(k, v); }
    say('review', 'Загрузка…');
    try {
      const r = await api('/requirements/review?' + p);
      if (!r.ok) { say('review', await errText(r), true); return; }
      const d = await r.json();
      rv.canEdit = d.can_edit; rv.total = d.total;
      $('px-rv-backlog').classList.toggle('d-none', !d.can_edit);
      $('px-rv-reextract').classList.toggle('d-none', !d.can_edit);
      if (!append) rv.lastTopic = undefined;
      const html = d.items.map((it) => {
        const head = it.topic !== rv.lastTopic ? `<div class="px-topic small fw-semibold text-muted mt-3 mb-1">${esc(it.topic ? 'Тема: ' + it.topic : 'Без темы')}</div>` : '';
        rv.lastTopic = it.topic;
        return head + reqHtml(it);
      }).join('');
      box.insertAdjacentHTML('beforeend', html || (append ? '' : '<div class="text-muted small">По этим условиям требований нет.</div>'));
      rv.offset += d.items.length;
      $('px-rv-more').classList.toggle('d-none', rv.offset >= d.total);
      setCount('review', d.counts.needs_human, 'ждут вас');
      $('px-rv-quality').textContent = d.quality ? d.quality.line : '';
      if (d.spot) {
        say('review', `Выборочная проверка недели: отмечено ${d.spot.done} из ${d.spot.size}. Отметка нужна для оценки точности разбора — пункт она не подтверждает и из «Ждут вас» не убирает.`);
        updateBatch();
        return;
      }
      say('review', `Показано ${rv.offset} из ${d.total}. Всего требований: ${d.counts.total}; ждут вас: ${d.counts.needs_human}; без цитаты источника: ${d.counts.unsourced || 0}; подтверждено моделью: ${d.counts.model_confirmed}; отклонено моделью: ${d.counts.model_rejected}; устарело: ${d.counts.superseded || 0}; склеено дублей: ${d.counts.merged || 0}.` + (d.can_edit ? '' : ' Вы можете только смотреть: подтверждает администратор.'));
      updateBatch();
    } catch (e) { say('review', 'Не удалось загрузить требования', true); }
  }

  function updateBatch() {
    const n = rv.selected.size;
    $('px-rv-batch').classList.toggle('d-none', !n);
    $('px-rv-batch').classList.toggle('d-flex', !!n);
    $('px-rv-selected').textContent = `Выбрано: ${n}`;
  }

  function inlineForm(id, html) { const el = document.querySelector(`[data-inline="${CSS.escape(id)}"]`); el.innerHTML = html; el.classList.remove('d-none'); return el; }

  async function reviewAction(action, id, body) {
    const r = await post(`/requirements/${encodeURIComponent(id)}/${action}`, body);
    if (!r.ok) { say('review', await errText(r), true); return false; }
    return true;
  }

  $('px-rv-list').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-rv]');
    if (!btn) return;
    const id = btn.dataset.id, act = btn.dataset.rv;
    if (act === 'confirm') {
      await PrimaryAnalitics.guardButton(btn, async () => {
        let r = await post(`/requirements/${encodeURIComponent(id)}/confirm`, {});
        if (r.status === 409) {  // модель считает пункт устаревшим или дублем (А6): нужно явное согласие
          const msg = (await r.json().catch(() => ({}))).detail || 'Модель считает пункт устаревшим. Подтвердить всё равно?';
          if (!confirm(msg)) return;
          r = await post(`/requirements/${encodeURIComponent(id)}/confirm`, { force: true });
        }
        if (!r.ok) { say('review', await errText(r), true); return; }
        await loadReview(false); loadSummary();
      });
      return;
    }
    if (act === 'current') {
      await PrimaryAnalitics.guardButton(btn, async () => {
        const r = await post(`/requirements/entity/${encodeURIComponent(btn.dataset.entity)}/mark-current`, {});
        if (!r.ok) { say('review', await errText(r), true); return; }
        await loadReview(false); loadSummary();
        say('review', 'Вердикт модели снят: пункт снова ждёт вас.');
      });
      return;
    }
    if (act === 'undo') {
      await PrimaryAnalitics.guardButton(btn, async () => {
        const r = await post(`/requirements/entity/${encodeURIComponent(btn.dataset.entity)}/undo-model`, {});
        if (!r.ok) { say('review', await errText(r), true); return; }
        await loadReview(false); loadSummary();
        say('review', 'Решение модели отменено.');
      });
      return;
    }
    if (act === 'spot-ok') {
      await PrimaryAnalitics.guardButton(btn, async () => {
        const r = await post(`/spot-check/${encodeURIComponent(btn.dataset.entity)}`, { verdict: 'ok' });
        if (!r.ok) { say('review', await errText(r), true); return; }
        await loadReview(false);
      });
      return;
    }
    if (act === 'spot-wrong') {
      const el = inlineForm(id, `<div class="px-form-row"><input class="form-control form-control-sm" placeholder="Что не так (необязательно)" maxlength="500"><button type="button" class="btn btn-sm btn-danger" data-do="spot-wrong" data-entity="${esc(btn.dataset.entity)}">Неверно</button><button type="button" class="btn btn-sm btn-link" data-do="cancel">Отмена</button></div>`);
      el.querySelector('input').focus();
      return;
    }
    if (act === 'reject') {
      const el = inlineForm(id, `<div class="px-form-row"><input class="form-control form-control-sm" placeholder="Причина (необязательно)" maxlength="500"><button type="button" class="btn btn-sm btn-danger" data-do="reject">Отклонить</button><button type="button" class="btn btn-sm btn-link" data-do="cancel">Отмена</button></div><div class="form-text">Требование исчезнет из выдачи агентам; данные не удаляются.</div>`);
      el.querySelector('input').focus();
    }
    if (act === 'status') {
      const opts = Object.entries(STATUS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('');
      const el = inlineForm(id, `<div class="px-form-row"><select class="form-select form-select-sm">${opts}</select><input class="form-control form-control-sm" placeholder="Причина (обязательна)" maxlength="500"><button type="button" class="btn btn-sm btn-primary" data-do="status">Сохранить</button><button type="button" class="btn btn-sm btn-link" data-do="cancel">Отмена</button></div>`);
      el.querySelector('input').focus();
    }
  });
  $('px-rv-list').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-do]');
    if (!btn) return;
    const holder = btn.closest('[data-inline]'); const id = holder.dataset.inline;
    if (btn.dataset.do === 'cancel') { holder.classList.add('d-none'); holder.innerHTML = ''; return; }
    const reason = holder.querySelector('input').value.trim();
    if (btn.dataset.do === 'spot-wrong') {
      await PrimaryAnalitics.guardButton(btn, async () => {
        const r = await post(`/spot-check/${encodeURIComponent(btn.dataset.entity)}`, { verdict: 'wrong', reason });
        if (!r.ok) { say('review', await errText(r), true); return; }
        await loadReview(false);
      });
      return;
    }
    await PrimaryAnalitics.guardButton(btn, async () => {
      const body = btn.dataset.do === 'status' ? { status: holder.querySelector('select').value, reason } : { reason };
      if (await reviewAction(btn.dataset.do, id, body)) { await loadReview(false); loadSummary(); }
    });
  });
  $('px-rv-list').addEventListener('change', (ev) => {
    const c = ev.target.closest('.px-rv-check');
    if (!c) return;
    c.checked ? rv.selected.add(c.dataset.id) : rv.selected.delete(c.dataset.id);
    updateBatch();
  });
  $('px-rv-confirm-selected').addEventListener('click', (ev) => PrimaryAnalitics.guardButton(ev.currentTarget, async () => {
    const r = await post('/requirements/confirm', { ids: [...rv.selected] });
    if (!r.ok) { say('review', await errText(r), true); return; }
    const d = await r.json();
    await loadReview(false); loadSummary();
    say('review', `Подтверждено: ${d.confirmed.length}` + (d.failed.length ? `, не удалось: ${d.failed.length}` : ''), d.failed.length > 0);
  }));
  $('px-rv-more').addEventListener('click', () => loadReview(true));
  $('px-rv-reextract').addEventListener('click', (ev) => {
    if (!confirm('Заново разобрать все материалы проекта по новой схеме? Идёт фоном (десятки минут), расшифровке не мешает. Ваши подтверждения и отказы переносятся на новые пункты; что не перенеслось — будет в отчёте.')) return;
    PrimaryAnalitics.guardButton(ev.currentTarget, async () => {
      const r = await post('/requirements/reextract', {});
      if (!r.ok) { say('review', await errText(r), true); return; }
      const d = await r.json();
      say('review', `Поставлено в очередь: ${d.jobs_queued} из ${d.materials} материалов. Ваших решений к переносу: ${d.human_decisions}. Список обновится по мере разбора.`);
    });
  });
  $('px-rv-backlog').addEventListener('click', (ev) => {
    if (!confirm('Поставить в очередь фоновую проверку моделью всех накопленных требований и конфликтов проекта? Расшифровке она не мешает; решения модели можно вернуть.')) return;
    PrimaryAnalitics.guardButton(ev.currentTarget, async () => {
      const r = await post('/auto-review/backlog', { scope: 'all' });
      if (!r.ok) { say('review', await errText(r), true); return; }
      const d = await r.json();
      const mode = d.mode === 'live' ? 'с применением' : 'в тени (без последствий)';
      say('review', `Поставлено задач: ${d.jobs_queued} (${mode}). Требований: ${d.requirements}, конфликтов: ${d.conflicts}, примерно ${d.estimate_minutes} мин.`);
    });
  });
  let rvTimer;
  document.querySelectorAll('#px-card-review .px-filter').forEach((f) => f.addEventListener(f.tagName === 'INPUT' ? 'input' : 'change', () => { clearTimeout(rvTimer); rvTimer = setTimeout(() => loadReview(false), 250); }));

  /* ---------- Обмен с агентами ---------- */
  let exCanEdit = false;
  document.querySelectorAll('[data-px-tab]').forEach((b) => b.addEventListener('click', () => {
    document.querySelectorAll('[data-px-tab]').forEach((x) => x.classList.toggle('active', x === b));
    for (const t of ['questions', 'verdicts', 'journal']) $('px-tab-' + t).classList.toggle('d-none', t !== b.dataset.pxTab);
    ({ questions: loadQuestions, verdicts: loadVerdicts, journal: loadJournal })[b.dataset.pxTab]();
  }));

  const Q_STATUS = { pending: 'ждёт ответа', drafted: 'есть черновик модели', answered: 'отвечено' };

  function questionHtml(q) {
    const src = (q.sources || []).map((s) => `<li><span class="text-muted">[${s.n}] ${esc(s.artifact)}</span> ${esc(s.text).slice(0, 300)}</li>`).join('');
    const answered = q.status === 'answered';
    const edit = exCanEdit && !answered ? `
      ${q.draft ? `<div class="px-draft"><div class="small text-muted">Черновик локальной модели (агенту не виден):</div><div class="px-statement">${esc(q.draft)}</div><button type="button" class="btn btn-sm btn-primary mt-1" data-q="approve" data-id="${q.id}">Одобрить черновик</button></div>` : ''}
      <textarea class="form-control form-control-sm mt-2" rows="3" maxlength="4000" placeholder="Ваш ответ агенту" data-q-text="${q.id}"></textarea>
      <div class="px-actions"><button type="button" class="btn btn-sm btn-primary" data-q="answer" data-id="${q.id}">Ответить</button>
      <button type="button" class="btn btn-sm btn-outline-secondary" data-q="draft" data-id="${q.id}" data-idle-label="Черновик локальной модели">Черновик локальной модели</button></div>` : '';
    return `<div class="px-item">
      <div class="d-flex flex-wrap gap-2 align-items-center mb-1"><code class="small">№${esc(q.request_id)}</code>
        <span class="badge ${answered ? 'text-bg-success' : 'text-bg-warning'}">${esc(Q_STATUS[q.status] || q.status)}</span>
        <span class="small text-muted">${esc(fmt(q.created_at))}</span></div>
      <div class="px-statement">${esc(q.question)}</div>
      ${answered ? `<div class="px-answer"><div class="small text-muted">Ответ (${esc(q.answer_author || '')}):</div>${esc(q.answer)}</div>` : ''}
      ${src ? `<details class="mt-1"><summary class="small text-muted">Найденные материалы (${q.sources.length})</summary><ul class="small px-sources">${src}</ul></details>` : ''}
      ${edit}</div>`;
  }

  async function loadQuestions() {
    const box = $('px-q-list');
    say('exchange', 'Загрузка…');
    const r = await api('/agent-inbox');
    if (!r.ok) { say('exchange', await errText(r), true); return; }
    const items = (await r.json()).items;
    const s = await api('/agent-exchange/summary').then((x) => x.ok ? x.json() : null).catch(() => null);
    exCanEdit = window.PX.role === 'admin';
    box.innerHTML = items.map(questionHtml).join('') || '<div class="text-muted small">Вопросов от агентов пока нет.</div>';
    say('exchange', exCanEdit ? '' : 'Вы можете только смотреть: отвечает администратор.');
    if (s) loadSummary();
  }
  $('px-q-list').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-q]');
    if (!btn) return;
    const id = btn.dataset.id, act = btn.dataset.q;
    await PrimaryAnalitics.guardButton(btn, async () => {
      let r;
      if (act === 'draft') r = await post(`/agent-inbox/${id}/draft`, {});
      else if (act === 'approve') r = await post(`/agent-inbox/${id}/answer`, { approve_draft: true });
      else {
        // Текст берём из той же карточки, что и кнопка (не первый textarea в документе).
        const box = btn.closest('.px-item');
        const field = (box && box.querySelector(`[data-q-text="${id}"]`)) || document.querySelector(`[data-q-text="${id}"]`);
        const text = (field && field.value || '').trim();
        if (!text) { say('exchange', 'Напишите ответ', true); return; }
        r = await post(`/agent-inbox/${id}/answer`, { text });
      }
      if (!r.ok) { say('exchange', await errText(r), true); return; }
      say('exchange', act === 'draft' ? 'Черновик готов: проверьте и одобрите.' : 'Ответ отправлен агенту.');
      await loadQuestions(); loadSummary();
    }, { label: act === 'draft' ? 'Модель пишет…' : undefined });
  });

  function verdictHtml(v) {
    const done = !!v.human_review;
    const list = (label, arr) => arr && arr.length ? `<div class="small"><span class="text-muted">${label}:</span> ${arr.map((x) => esc(typeof x === 'string' ? x : JSON.stringify(x))).join('; ')}</div>` : '';
    const acts = exCanEdit && v.needs_review && v.is_active ? `<div class="px-actions"><button type="button" class="btn btn-sm btn-primary" data-v="confirm" data-id="${v.id}">Подтвердить</button><button type="button" class="btn btn-sm btn-outline-danger" data-v="reject" data-id="${v.id}">Отклонить</button></div>` : '';
    return `<div class="px-item"><div class="d-flex flex-wrap gap-2 align-items-center mb-1">
        <code class="small">${esc(v.requirement_id)}</code>
        <span class="badge text-bg-info">${esc(VERDICT[v.verdict] || v.verdict)}</span><span class="small text-muted">уверенность: ${esc(v.confidence)}</span>
        ${done ? `<span class="badge ${v.is_active ? 'text-bg-primary' : 'text-bg-secondary'}">${v.is_active ? 'подтверждён' : 'отклонён'}: ${esc(v.human_review.by)}</span>` : (v.needs_review ? '<span class="badge text-bg-warning">не проверен человеком</span>' : '')}
        ${!v.is_active && !done ? '<span class="badge text-bg-secondary">заменён новым</span>' : ''}</div>
      ${list('Препятствия', v.blockers)}${list('Вопросы заказчику', v.questions_for_customer)}${list('Доказательства в коде', (v.code_evidence || []).map((c) => `${c.object || ''} ${c.path || ''} ${c.note || ''}`.trim()))}${acts}</div>`;
  }
  async function loadVerdicts() {
    const r = await api('/agent-verdicts');
    if (!r.ok) { say('exchange', await errText(r), true); return; }
    const d = await r.json();
    exCanEdit = d.can_edit;
    $('px-v-list').innerHTML = d.items.map(verdictHtml).join('') || '<div class="text-muted small">Вердиктов от агентов пока нет.</div>';
    say('exchange', 'Вердикты агента помечены как непроверенные, пока вы их не подтвердите. Машинная проверка по коду не заменяет подтверждение человеком.');
  }
  $('px-v-list').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-v]');
    if (!btn) return;
    if (btn.dataset.v === 'reject' && !confirm('Отклонить этот вердикт? Он перестанет учитываться, но останется в базе.')) return;
    await PrimaryAnalitics.guardButton(btn, async () => {
      const r = await post(`/agent-verdicts/${btn.dataset.id}/${btn.dataset.v}`, {});
      if (!r.ok) { say('exchange', await errText(r), true); return; }
      await loadVerdicts(); loadSummary();
    });
  });

  async function loadJournal() {
    const p = new URLSearchParams({ limit: 100 });
    if ($('px-j-decision').value) p.set('decision', $('px-j-decision').value);
    if ($('px-j-source').value) p.set('source', $('px-j-source').value);
    const r = await api('/agent-exchange?' + p);
    if (!r.ok) { say('exchange', await errText(r), true); return; }
    const items = (await r.json()).items;
    $('px-j-body').innerHTML = items.map((j) => `<tr><td class="text-nowrap">${esc(fmt(j.at))}</td><td>${esc(j.agent || j.source)}</td><td>${esc(j.kind)}</td><td>${esc(j.request_id ?? '')}</td>
      <td><span class="badge ${j.decision === 'accepted' ? 'text-bg-success' : (j.decision === 'rejected' ? 'text-bg-danger' : 'text-bg-warning')}">${esc(DECISION[j.decision] || j.decision)}</span></td>
      <td class="small">${esc((j.reasons || []).join('; ')).slice(0, 200)}</td></tr>`).join('') || '<tr><td colspan="6" class="text-muted small">Записей нет.</td></tr>';
    say('exchange', '');
  }
  ['px-j-decision', 'px-j-source'].forEach((id) => $(id).addEventListener('change', loadJournal));

  /* ---------- Настройки ---------- */
  let settingsCanEdit = false;
  async function loadSettings() {
    const r = await api('/agent-settings');
    if (!r.ok) { say('settings', await errText(r), true); return; }
    const d = await r.json(), s = d.settings;
    settingsCanEdit = d.can_edit;
    $('px-s-tz').value = s.timezone;
    $('px-s-pack').innerHTML = d.packs.map((p) => `<option value="${esc(p.name)}" data-desc="${esc(p.description)}">${esc(p.title)}</option>`).join('');
    $('px-s-pack').value = s.domain_pack;
    const desc = () => { const o = $('px-s-pack').selectedOptions[0]; $('px-s-pack-desc').textContent = o ? o.dataset.desc : ''; };
    $('px-s-pack').onchange = desc; desc();
    $('px-s-participants').value = (s.participants || []).map((p) => `${p.name} = ${p.side === 'customer' ? 'заказчик' : 'исполнитель'}`).join('\n');
    $('px-s-hint-customer').value = (s.side_hints.customer || []).join(', ');
    $('px-s-hint-vendor').value = (s.side_hints.vendor || []).join(', ');
    $('px-s-enabled').checked = !!s.exchange_enabled;
    $('px-s-key-scope').value = s.default_key_scope || 'read';
    document.querySelectorAll('#px-settings-form input, #px-settings-form select, #px-settings-form textarea').forEach((el) => { el.disabled = !settingsCanEdit; });
    $('px-s-save').classList.toggle('d-none', !settingsCanEdit);
    say('settings', settingsCanEdit ? '' : 'Вы можете только смотреть: менять настройки может администратор.');
  }
  function parseParticipants(text) {
    const rows = [];
    for (const line of text.split('\n')) {
      const t = line.trim();
      if (!t) continue;
      const m = t.match(/^(.+?)\s*[=:—-]\s*(заказчик|исполнитель)\s*$/i);
      if (!m) throw new Error(`Не понял строку: «${t}». Формат: «Имя = заказчик» или «Имя = исполнитель».`);
      rows.push({ name: m[1].trim(), side: m[2].toLowerCase() === 'заказчик' ? 'customer' : 'vendor' });
    }
    return rows;
  }
  $('px-settings-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    await PrimaryAnalitics.guardButton($('px-s-save'), async () => {
      let body;
      try {
        const words = (id) => $(id).value.split(',').map((x) => x.trim()).filter(Boolean);
        body = { timezone: $('px-s-tz').value.trim(), domain_pack: $('px-s-pack').value, participants: parseParticipants($('px-s-participants').value),
          side_hints: { customer: words('px-s-hint-customer'), vendor: words('px-s-hint-vendor') }, exchange_enabled: $('px-s-enabled').checked, default_key_scope: $('px-s-key-scope').value };
      } catch (e) { say('settings', e.message, true); return; }
      const r = await fetch(`/projects/${PID}/agent-settings`, { method: 'PUT', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${PrimaryAnalitics.token()}` }, body: JSON.stringify(body) });
      if (!r.ok) { say('settings', await errText(r), true); return; }
      say('settings', 'Сохранено. Агенты увидят изменения при следующем обращении.');
    });
  });

  /* ---------- Вопросы к заказчику и карточка проекта (REQUIREMENTS-PLAN А7, этап 3) ---------- */
  async function loadOpenQuestions() {
    const box = $('px-oq-list');
    say('oq', 'Загрузка…');
    const r = await api('/open-questions');
    if (!r.ok) { say('oq', await errText(r), true); return; }
    const d = await r.json();
    setCount('oq', d.total);
    say('oq', d.total ? '' : 'Открытых вопросов нет (у материалов, разобранных до новой схемы, их нет, пока материал не переизвлечён).');
    box.innerHTML = d.items.map((q) => {
      const src = (q.evidence || [])[0];
      const opts = (q.options || []).length ? `<ul class="small mb-1">${q.options.map((o) => `<li>${esc(o)}</li>`).join('')}</ul>` : '';
      return `<div class="px-item"><div class="small text-muted">${esc(q.topic ? 'Тема: ' + q.topic : 'Без темы')}</div>
        <div class="px-statement">${esc(q.question)}</div>${opts}
        ${src ? `<blockquote class="px-quote">«${esc(src.quote || '')}»<footer>${whoWhen(src)}</footer></blockquote>` : ''}</div>`;
    }).join('');
  }

  async function loadCard() {
    const r = await api('/card');
    if (!r.ok) { say('card', await errText(r), true); return; }
    const d = await r.json();
    $('px-card-text').value = d.text || '';
    $('px-card-text').readOnly = !d.can_edit;
    $('px-card-actions').classList.toggle('d-none', !d.can_edit);
    const m = d.meta || {};
    $('px-card-meta').textContent = !d.text ? 'Карточки пока нет: она соберётся при разборе следующего материала или по кнопке.'
      : m.source === 'human' ? `Правка человека: ${m.by || ''}, ${fmt(m.at)}` : `Черновик модели, ${fmt(m.at)}`;
  }
  $('px-card-save').addEventListener('click', (ev) => PrimaryAnalitics.guardButton(ev.currentTarget, async () => {
    const text = $('px-card-text').value.trim();
    if (!text) { say('card', 'Карточка пустая', true); return; }
    const r = await api('/card', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
    if (!r.ok) { say('card', await errText(r), true); return; }
    say('card', 'Сохранено. Модель увидит карточку при разборе следующих материалов.');
    loadCard();
  }));
  $('px-card-rebuild').addEventListener('click', (ev) => PrimaryAnalitics.guardButton(ev.currentTarget, async () => {
    say('card', 'Ставлю в очередь…');
    let r = await post('/card/rebuild', {});
    if (r.status === 409 && /правил человек/.test(await r.clone().text())) {
      if (!confirm('Карточку правил человек. Заменить правку новым черновиком модели?')) { say('card', ''); return; }
      r = await post('/card/rebuild', { discard_human: true });
    }
    if (!r.ok) { say('card', await errText(r), true); return; }
    say('card', 'Поставлено в очередь обработки: черновик появится, когда воркер дойдёт до задачи.');
  }));

  const LOADERS = { review: () => loadReview(false), exchange: loadQuestions, settings: loadSettings, oq: loadOpenQuestions, card: loadCard };
  loadSummary();
})();
