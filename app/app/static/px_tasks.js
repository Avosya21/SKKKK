/* Карточка «Задачи» на странице проекта (docs/plans/PORTAL-ACCURACY-MANAGER-PLAN.md, Ш4).
   Вкладки «Открытые / Просрочено / На неделе / Без срока / Сделано / Все», фильтр по ответственному, правка статуса,
   ответственного и срока прямо в строке, ручные задачи. Права приходят с сервера (can_edit: admin и manager проекта);
   все действия проверяются на сервере. Календарь остаётся в project.html (GET …/deadlines). */
(function () {
  const board = document.getElementById('tasks-board');
  if (!board || !window.PX) return;
  const PID = window.PX.projectId;
  const esc = (t) => String(t ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const VIEWS = [
    ['open', 'Открытые'], ['overdue', 'Просрочено'], ['week', 'На неделе'], ['no_deadline', 'Без срока'], ['reference', 'ТЗ и план'], ['done', 'Сделано'], ['all', 'Все'],
  ];
  const FIELD = { status: 'статус', assignee: 'ответственный', deadline_at: 'срок' };
  let STATUS_LABEL = {};
  const st = { view: 'open', assignee: '', offset: 0, items: [], data: null, adding: false };
  const LIMIT = 50;

  const api = async (path, opts) => {
    try {
      return await SKKKK.api(`/projects/${PID}${path}`, opts);
    } catch (e) {
      return new Response(JSON.stringify({ detail: 'Нет связи с сервером. Проверьте интернет и повторите.' }), { status: 599, headers: { 'Content-Type': 'application/json' } });
    }
  };
  const send = (method, path, body) => api(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  const errText = async (r) => {
    try { const d = (await r.json()).detail; return typeof d === 'string' ? d : JSON.stringify(d); } catch (e) { return 'Ошибка ' + r.status; }
  };
  const statusEl = () => document.getElementById('deadlines-status');
  const say = (text, bad) => { const el = statusEl(); if (el) { el.textContent = text || ''; el.classList.toggle('text-danger', !!bad); } };
  const fmtDate = (iso) => { try { return new Date(`${iso}T00:00:00`).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }); } catch (e) { return iso; } };
  const fmtAt = (iso) => { try { return new Date(iso).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }); } catch (e) { return iso || ''; } };

  function deadlineHtml(t) {
    if (!t.deadline && !t.deadline_date) return '<span class="text-muted">без срока</span>';
    const exact = t.deadline_date ? (t.date_approximate ? '≈ ' : '') + fmtDate(t.deadline_date) : '';
    const said = t.deadline && t.source_kind !== 'manual' && !(t.edited && t.edited.fields.includes('deadline_at')) ? t.deadline : '';
    const main = exact || esc(t.deadline);
    const text = `${esc(main)}${said && exact ? `<span class="text-muted small"> · «${esc(said)}»</span>` : ''}`;
    return t.overdue ? `<span class="deadline-overdue" title="Просрочено">${text}</span>` : text;
  }

  function editedHtml(t) {
    if (t.source_kind === 'manual') return '<span class="badge text-bg-light border">добавлена вручную</span>';
    const origin = t.origin_label ? `<span class="badge text-bg-light border" title="Положение документа или этап плана, а не поручение: не входит в открытые и просроченные">${esc(t.origin_label)}</span>` : '';
    if (!t.edited) return origin;
    const label = (k, v) => (k === 'status' ? (STATUS_LABEL[v] || v) : v);
    const was = Object.entries(t.edited.before || {}).filter(([k]) => FIELD[k])
      .map(([k, v]) => `${FIELD[k]}: ${v === null || v === undefined || v === '' ? '—' : label(k, v)}`).join('; ');
    return origin + `<span class="badge text-bg-light border" title="${esc(was ? 'Было: ' + was : '')}">изменено вручную: ${esc(t.edited.by || '')}, ${esc(fmtAt(t.edited.at))}</span>`;
  }

  function sourceHtml(t) {
    if (t.source_kind === 'manual') return '';
    const name = esc(t.filename || t.artifact_id || '');
    if (!name) return '';
    return t.transcript_html_url
      ? `<a class="small" href="${esc(t.transcript_html_url)}" target="_blank" rel="noopener noreferrer">${name}</a>`
      : `<span class="small text-muted">${name}</span>`;
  }

  function taskHtml(t, canEdit, statuses) {
    const statusCtl = canEdit
      ? `<select class="form-select form-select-sm px-task-status" data-field="status" aria-label="Статус задачи">${statuses.map((s) => `<option value="${s.key}"${s.key === t.status ? ' selected' : ''}>${esc(s.label)}</option>`).join('')}</select>`
      : `<span class="badge ${t.status === 'done' ? 'text-bg-success' : t.status === 'cancelled' ? 'text-bg-dark' : t.status === 'in_progress' ? 'text-bg-info' : 'text-bg-primary'}">${esc(t.status_label)}</span>`;
    const assigneeCtl = canEdit
      ? `<input class="form-control form-control-sm px-task-assignee" data-field="assignee" list="px-task-people" value="${esc(t.assignee || '')}" placeholder="Ответственный" aria-label="Ответственный" maxlength="200">`
      : `<span class="small">${esc(t.assignee || '—')}</span>`;
    const dateCtl = canEdit
      ? `<input type="date" class="form-control form-control-sm px-task-date" data-field="deadline_at" value="${esc(t.deadline_date || '')}" aria-label="Срок">`
      : '';
    const del = canEdit && t.source_kind === 'manual'
      ? `<button type="button" class="btn btn-sm btn-link text-danger p-0 px-task-del" title="Убрать задачу">убрать</button>` : '';
    return `<div class="px-item px-task${t.overdue ? ' px-task-overdue' : ''}" data-task="${esc(t.id)}">
      <div class="px-statement">${esc(t.description)}</div>
      <div class="px-task-meta">
        <div class="px-task-cell">${statusCtl}</div>
        <div class="px-task-cell px-task-grow">${assigneeCtl}</div>
        <div class="px-task-cell">${dateCtl}</div>
        <div class="px-task-cell small">${deadlineHtml(t)}</div>
      </div>
      <div class="d-flex flex-wrap gap-2 align-items-center mt-1">${editedHtml(t)}${sourceHtml(t)}${del}</div>
    </div>`;
  }

  function toolbarHtml(d) {
    const tabs = VIEWS.map(([key, label]) => {
      const n = d.counts[key] || 0;
      return `<button type="button" class="btn btn-sm ${key === st.view ? 'btn-primary' : 'btn-outline-secondary'}" data-view="${key}" aria-pressed="${key === st.view}">${esc(label)}${n ? ` <span class="badge ${key === 'overdue' ? 'text-bg-danger' : 'text-bg-light'}">${n}</span>` : ''}</button>`;
    }).join('');
    const people = d.assignees.map((a) => `<option value="${esc(a)}"${a === st.assignee ? ' selected' : ''}>${esc(a)}</option>`).join('');
    return `<div class="d-flex flex-wrap gap-2 align-items-center mb-2">
        <div class="d-flex flex-wrap gap-1" role="group" aria-label="Какие задачи показать">${tabs}</div>
        <select class="form-select form-select-sm px-task-who" aria-label="Ответственный"><option value="">Все ответственные</option>${people}</select>
        ${d.can_edit ? '<button type="button" class="btn btn-sm btn-outline-primary px-task-add">+ Задача</button>' : ''}
      </div>
      <datalist id="px-task-people">${d.assignees.map((a) => `<option value="${esc(a)}">`).join('')}</datalist>
      <div class="px-task-new d-none"></div>`;
  }

  function newFormHtml() {
    return `<div class="px-form-row flex-wrap mb-2">
      <input class="form-control form-control-sm px-new-desc" placeholder="Что сделать" maxlength="500" aria-label="Что сделать">
      <input class="form-control form-control-sm px-new-who" list="px-task-people" placeholder="Ответственный" maxlength="200" aria-label="Ответственный">
      <input type="date" class="form-control form-control-sm px-new-date" aria-label="Срок">
      <button type="button" class="btn btn-sm btn-primary px-new-save">Добавить</button>
      <button type="button" class="btn btn-sm btn-link px-new-cancel">Отмена</button>
    </div>`;
  }

  function setHeader(d) {
    const badge = document.getElementById('deadlines-count-badge');
    if (badge) {
      const open = d.counts.open || 0;
      badge.textContent = `открыто ${open}${d.counts.overdue ? ` · просрочено ${d.counts.overdue}` : ''}`;
      badge.className = `badge border ${d.counts.overdue ? 'text-bg-danger' : 'text-bg-light text-dark'}`;
    }
    say(d.counts.all
      ? `Открытых ${d.counts.open} · просрочено ${d.counts.overdue} · на ближайшей неделе ${d.counts.week} · без срока ${d.counts.no_deadline} · из ТЗ и плана ${d.counts.reference} · сделано и отменено ${d.counts.done}`
      : 'Задачи появятся, когда в материалах прозвучат поручения' + (d.can_edit ? ', или добавьте задачу вручную.' : '.'));
  }

  async function load(append) {
    if (!append) { st.offset = 0; st.items = []; }
    const p = new URLSearchParams({ view: st.view, offset: st.offset, limit: LIMIT });
    if (st.assignee) p.set('assignee', st.assignee);
    const r = await api('/tasks?' + p);
    if (!r.ok) { say(await errText(r), true); return; }
    const d = await r.json();
    st.data = d;
    STATUS_LABEL = Object.fromEntries((d.statuses || []).map((s) => [s.key, s.label]));
    st.items = st.items.concat(d.items);
    st.offset = st.items.length;
    setHeader(d);
    const list = st.items.length
      ? st.items.map((t) => taskHtml(t, d.can_edit, d.statuses)).join('')
      : '<div class="text-muted small">В этой вкладке задач нет.</div>';
    board.innerHTML = toolbarHtml(d) + `<div class="px-list">${list}</div>`
      + (st.offset < d.total ? '<button type="button" class="btn btn-sm btn-outline-secondary mt-2 px-task-more">Показать ещё</button>' : '');
    if (st.adding) openNewForm();
  }

  function openNewForm() {
    const box = board.querySelector('.px-task-new');
    if (!box) return;
    st.adding = true;
    box.innerHTML = newFormHtml();
    box.classList.remove('d-none');
    box.querySelector('.px-new-desc').focus();
  }

  async function saveField(row, field, value) {
    const id = row.dataset.task;
    const path = id.startsWith('m') ? `/manual-tasks/${id.slice(1)}` : `/tasks/${id.slice(1)}`;
    const r = await send('PATCH', path, { [field]: value });
    if (!r.ok) { say(await errText(r), true); return; }
    await load(false);
    document.dispatchEvent(new CustomEvent('px:tasks-changed'));
  }

  board.addEventListener('click', async (ev) => {
    const tab = ev.target.closest('[data-view]');
    if (tab) { st.view = tab.dataset.view; await load(false); return; }
    if (ev.target.closest('.px-task-more')) { await load(true); return; }
    if (ev.target.closest('.px-task-add')) { openNewForm(); return; }
    if (ev.target.closest('.px-new-cancel')) { st.adding = false; const box = board.querySelector('.px-task-new'); box.classList.add('d-none'); box.innerHTML = ''; return; }
    const save = ev.target.closest('.px-new-save');
    if (save) {
      const box = save.closest('.px-task-new');
      const body = { description: box.querySelector('.px-new-desc').value, assignee: box.querySelector('.px-new-who').value,
        deadline_at: box.querySelector('.px-new-date').value };
      await SKKKK.guardButton(save, async () => {
        const r = await send('POST', '/tasks', body);
        if (!r.ok) { say(await errText(r), true); return; }
        st.adding = false;
        await load(false);
        document.dispatchEvent(new CustomEvent('px:tasks-changed'));
      });
      return;
    }
    const del = ev.target.closest('.px-task-del');
    if (del) {
      if (!confirm('Убрать эту задачу из списка?')) return;
      const id = del.closest('[data-task]').dataset.task;
      const r = await send('DELETE', `/manual-tasks/${id.slice(1)}`);
      if (!r.ok) { say(await errText(r), true); return; }
      await load(false);
      document.dispatchEvent(new CustomEvent('px:tasks-changed'));
    }
  });
  board.addEventListener('change', async (ev) => {
    const who = ev.target.closest('.px-task-who');
    if (who) { st.assignee = who.value; await load(false); return; }
    const ctl = ev.target.closest('[data-field]');
    if (!ctl) return;
    ctl.disabled = true;
    await saveField(ctl.closest('[data-task]'), ctl.dataset.field, ctl.value);
  });
  board.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && ev.target.closest('.px-new-desc, .px-new-who')) { ev.preventDefault(); board.querySelector('.px-new-save')?.click(); }
  });

  window.PXTasks = { load: () => load(false) };
})();
