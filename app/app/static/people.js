/* Люди и стороны проекта (PLATFORM-FOUNDATION-PLAN, ПФ3): реестр людей, «Кто это?», стороны компаний.
   Права проверяются на сервере: админ и менеджер проекта правят, переключатель «брать стороны из списка» — только админ. */
(function () {
  const root = document.getElementById('pp-card');
  if (!root || !window.PX) return;
  const PID = window.PX.projectId;
  const $ = (id) => document.getElementById(id);
  const esc = (t) => String(t ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const SIDE = { customer: 'Заказчик', executor: 'Исполнитель', unknown: 'Сторона не определена' };
  const SOURCE = { human: 'указано вами', card: 'из карточки', project_party: 'из списка проекта', model: 'догадка по тексту' };
  const AUTH = { '': 'Может утверждать: по умолчанию', approver: 'Утверждает решения', participant: 'Участник', observer: 'Наблюдатель' };
  const KIND = { chat_author: 'автор переписки', speaker_label: 'голос на записи', guest: 'гость встречи', document_author: 'автор документа' };
  const ACTION = {
    create: 'добавлен человек', update: 'изменены данные', set_side: 'изменена сторона', merge: 'объединены люди', add_alias: 'добавлено написание',
    resolve_unknown: 'разобран вопрос «Кто это?»', set_party: 'сторона компании', platform_on: 'включено использование списка', platform_off: 'выключено использование списка',
    voice_consent: 'отмечено согласие на голос', voice_withdraw: 'отозвано согласие на голос', link_voice: 'голос привязан к учётке',
    prune: 'убрана лишняя запись', forget: 'человек удалён из проекта',
  };
  let state = { people: [], parties: [], members_without_person: [], unknown_open: 0, platform_enabled: false };

  const say = (text, bad) => { const el = $('pp-status'); if (el) { el.textContent = text || ''; el.classList.toggle('text-danger', !!bad); } };
  const errText = async (r) => {
    try { const d = (await r.json()).detail; return typeof d === 'string' ? d : JSON.stringify(d); } catch (e) { return 'Ошибка ' + r.status; }
  };
  const call = async (path, method, body) => {
    try {
      const r = await SKKKK.api(`/projects/${PID}${path}`, { method: method || 'GET', headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined });
      if (!r.ok) { say(await errText(r), true); return null; }
      return await r.json();
    } catch (e) { say('Нет связи с сервером. Проверьте интернет и повторите.', true); return null; }
  };
  const apply = (data) => { if (!data) return; state = { ...state, ...data }; render(); };
  const opts = (map, cur) => Object.entries(map).map(([k, v]) => `<option value="${esc(k)}"${k === (cur ?? '') ? ' selected' : ''}>${esc(v)}</option>`).join('');

  // Р1 (PEOPLE-FLOW-PLAN, ЛГ2): сторона человека — от его компании в проекте; у человека её правят, только пока компании нет в сторонах
  const partyOf = (company) => (company ? state.parties.find((x) => looseCompanyKey(x.company_name) === looseCompanyKey(company)) : null);

  function renderPeople() {
    const list = $('pp-list');
    if (!state.people.length) { list.innerHTML = '<div class="text-muted small">Пока никого. Нажмите «Найти людей в материалах» или добавьте вручную.</div>'; return; }
    list.innerHTML = state.people.map((p) => {
      const others = state.people.filter((o) => o.id !== p.id).map((o) => `<option value="${o.id}">${esc(o.display_name)}</option>`).join('');
      const extra = p.aliases.filter((a) => a !== p.display_name);
      const meta = [p.company_name, p.job_title, p.user ? `учётка: ${esc(p.user.name)}` : 'без учётки в портале', p.voices ? `голосов: ${p.voices}` : null].filter(Boolean).join(' · ');
      return `<div class="px-item" data-person="${p.id}">
        <div class="d-flex flex-wrap justify-content-between gap-2">
          <div><strong>${esc(p.display_name)}</strong>${p.side === 'unknown' ? ' <span class="badge text-bg-warning">сторона не определена</span>' : ''}
            <div class="small text-muted">${meta}</div>
            ${extra.length ? `<div class="small text-muted">Также пишут: ${extra.map(esc).join(', ')}</div>` : ''}</div>
        </div>
        <div class="px-form-row">
          <select class="form-select form-select-sm" data-act="side" aria-label="Сторона"${partyOf(p.company_name) ? ' disabled title="Сторона берётся от компании; меняется в «Стороны компаний»"' : ''}>${opts(SIDE, p.side)}</select>
          <select class="form-select form-select-sm" data-act="authority" aria-label="Право утверждать">${opts(AUTH, p.authority || '')}</select>
          <input type="text" class="form-control form-control-sm" data-act="company" placeholder="Компания из сторон проекта" list="pp-party-companies" value="${esc(p.company_name || '')}" maxlength="200">
        </div>
        <div class="small text-muted mt-1">${partyOf(p.company_name) ? `Сторона — от компании «${esc(partyOf(p.company_name).company_name)}» в проекте` : `Сторона: ${esc(SOURCE[p.side_source] || p.side_source)}`}</div>
        ${!p.external && (p.voice_suggestions || []).length ? `<div class="px-form-row"><span class="small text-muted">Найден голос с таким именем:</span>${p.voice_suggestions.map((v) =>
          `<button type="button" class="btn btn-sm btn-outline-secondary" data-act="link-voice" data-profile="${v.id}">Привязать к учётке: ${esc(v.name)}</button>`).join('')}</div>` : ''}
        ${p.external ? `<div class="px-form-row">${p.voice_consent_at
          ? `<span class="small text-success">Согласие на запись голоса получено ${esc(new Date(p.voice_consent_at).toLocaleDateString('ru-RU'))}</span>`
          : `<span class="small text-muted">Согласия на запись голоса нет: голос этого человека подтвердить нельзя.</span>
             <button type="button" class="btn btn-sm btn-outline-secondary" data-act="consent">Отметить: согласие получено</button>`}</div>` : ''}
        <div class="px-form-row">
          <input type="text" class="form-control form-control-sm" data-act="alias-text" placeholder="Ещё одно написание имени" maxlength="200">
          <button type="button" class="btn btn-sm btn-outline-secondary" data-act="alias">Добавить написание</button>
          ${window.PX.role === 'admin' ? '<button type="button" class="btn btn-sm btn-outline-danger" data-act="forget" title="Удалить человека, его написания и голоса проекта навсегда">Удалить</button>' : ''}
          ${others ? `<select class="form-select form-select-sm" data-act="merge-with" aria-label="Кого объединить"><option value="">Объединить с…</option>${others}</select>
          <button type="button" class="btn btn-sm btn-outline-danger" data-act="merge">Это один человек</button>` : ''}
        </div>
      </div>`;
    }).join('');
  }

  function renderUnknown() {
    const list = $('pp-unknown');
    $('pp-unknown-count').textContent = state.unknown_open ? `(ждут ответа: ${state.unknown_open})` : '';
    const badge = $('pp-badge');
    badge.classList.toggle('d-none', !state.unknown_open);
    badge.textContent = state.unknown_open ? `Кто это? ${state.unknown_open}` : '';
    if (!state.unknown_items || !state.unknown_items.length) {
      list.innerHTML = state.unknown_open ? '<div class="text-muted small">Загрузка…</div>' : '<div class="text-muted small">Все опознаны.</div>';
      return;
    }
    const persons = state.people.map((o) => `<option value="${o.id}">${esc(o.display_name)}</option>`).join('');
    list.innerHTML = state.unknown_items.map((u) => `<div class="px-item" data-unknown="${u.id}">
      <div><strong>${esc(u.label)}</strong> <span class="small text-muted">${esc(KIND[u.kind] || u.kind)}</span></div>
      ${(u.examples || []).map((e) => `<blockquote class="px-quote">${esc(e)}</blockquote>`).join('')}
      ${(u.suggestions || []).length ? `<div class="small text-muted">Возможно: ${u.suggestions.map((s) => esc(s.name)).join(', ')}</div>` : ''}
      <div class="px-form-row">
        <select class="form-select form-select-sm" data-act="who"><option value="">Выберите человека…</option>${persons}</select>
        <button type="button" class="btn btn-sm btn-primary" data-act="resolve">Это он</button>
        <button type="button" class="btn btn-sm btn-outline-secondary" data-act="new">Новый человек</button>
        <button type="button" class="btn btn-sm btn-outline-secondary" data-act="ignore">Не участник</button>
      </div></div>`).join('');
  }

  // Компания стороны — из справочника портала; новая добавится туда сама (PEOPLE-FLOW-PLAN, ЛГ1)
  const looseCompanyKey = (name) => String(name || '').toLowerCase().replace(/ё/g, 'е')
    .replace(/(^|[^0-9a-zа-я])(ооо|оао|зао|пао|ао|ип|llc|ltd|inc)(?=$|[^0-9a-zа-я])/g, ' ').replace(/[^0-9a-zа-я]/g, '');
  function partyHint() {
    const value = $('pp-party-name').value.trim();
    const hit = (state.companies || []).find((c) => looseCompanyKey(c.name) === looseCompanyKey(value));
    $('pp-party-hint').textContent = !value ? '' : hit
      ? (hit.name === value ? 'Есть в справочнике.' : `В справочнике: «${hit.name}» — будет записана так.`)
      : 'Новая компания — добавится в справочник портала с выбранной стороной.';
  }

  function renderParties() {
    $('pp-companies').innerHTML = (state.companies || []).map((c) => `<option value="${esc(c.name)}"></option>`).join('');
    $('pp-party-companies').innerHTML = state.parties.map((p) => `<option value="${esc(p.company_name)}"></option>`).join('');
    $('pp-parties').innerHTML = state.parties.length
      ? state.parties.map((p) => `<div class="px-item d-flex justify-content-between"><span>${esc(p.company_name)}</span><span class="text-muted">${esc(SIDE[p.side] || p.side)}</span></div>`).join('')
      : '<div class="text-muted small">Стороны компаний не заданы.</div>';
  }

  function renderPlatform() {
    const box = $('pp-platform');
    box.checked = !!state.platform_enabled;
    box.disabled = window.PX.role !== 'admin';
    $('pp-platform-hint').textContent = state.platform_enabled
      ? 'Включено: в анализе проекта сторона человека берётся из этого списка. Если человека нет в списке, работает прежняя догадка по роли.'
      : 'Пока выключено, стороны определяются по ролям из текста, как раньше. Включает администратор.';
  }

  function renderMembers() {
    const rows = state.members_without_person || [];
    $('pp-members-row').classList.toggle('d-none', !rows.length);
    $('pp-member-select').innerHTML = rows.map((m) => `<option value="${m.user_id}">${esc(m.name)}</option>`).join('');
  }

  function render() {
    renderPeople(); renderUnknown(); renderParties(); renderPlatform(); renderMembers();
    const count = state.people.length;
    const tabCount = $('pp-tabcount');  // число людей — на вкладке «Люди и стороны» блока «О проекте»
    if (tabCount) { tabCount.textContent = count; tabCount.classList.toggle('d-none', !count); }
  }

  async function loadAll() {
    say('Загрузка…');
    const data = await call('/people');
    if (!data) return;
    say('');
    state = { ...state, ...data };
    if (data.unknown_open) {
      const u = await call('/people/unknown');
      state.unknown_items = u ? u.items : [];
    } else { state.unknown_items = []; }
    render();
  }

  async function reloadUnknown() {
    const u = await call('/people/unknown');
    state.unknown_items = u ? u.items : [];
    render();
  }

  $('pp-seed').addEventListener('click', async (e) => {
    e.target.disabled = true; say('Ищу людей в материалах…');
    const r = await call('/people/seed', 'POST');
    e.target.disabled = false;
    if (r) { apply(r); await reloadUnknown(); say([r.created ? `Добавлено людей: ${r.created}. Проверьте стороны.` : 'Новых людей не найдено.', r.pruned ? `Убрано лишних записей (метки спикеров, роли): ${r.pruned}.` : ''].filter(Boolean).join(' ')); }
  });
  $('pp-toggle-add').addEventListener('click', () => $('pp-add-form').classList.toggle('d-none'));
  $('pp-new-company').addEventListener('input', () => {  // компания из сторон проекта — сторона подставляется и не правится
    const party = partyOf($('pp-new-company').value.trim());
    if (party) $('pp-new-side').value = party.side;
    $('pp-new-side').disabled = Boolean(party);
  });
  $('pp-add-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const r = await call('/people', 'POST', { display_name: $('pp-new-name').value.trim(), company_name: $('pp-new-company').value.trim() || null, side: $('pp-new-side').value });
    if (r) { apply(r); e.target.reset(); $('pp-new-side').disabled = false; e.target.classList.add('d-none'); say('Добавлен.'); }
  });
  $('pp-member-add').addEventListener('click', async () => {
    const id = Number($('pp-member-select').value);
    if (!id) return;
    const r = await call('/people/from-user', 'POST', { user_id: id });
    if (r) { apply(r); say('Сотрудник добавлен.'); }
  });
  $('pp-party-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const r = await call('/parties', 'PUT', { company_name: $('pp-party-name').value.trim(), side: $('pp-party-side').value });
    if (r) { apply(r); e.target.reset(); partyHint(); say('Сохранено.'); }
  });
  $('pp-party-name').addEventListener('input', partyHint);
  $('pp-platform').addEventListener('change', async (e) => {
    const r = await call('/people-platform', 'PUT', { enabled: e.target.checked });
    if (r) apply(r); else e.target.checked = !e.target.checked;
  });

  $('pp-list').addEventListener('change', async (e) => {
    const card = e.target.closest('[data-person]'); if (!card) return;
    const id = card.dataset.person, act = e.target.dataset.act;
    let body = null;
    if (act === 'side') body = { side: e.target.value };
    else if (act === 'authority') body = e.target.value ? { authority: e.target.value } : { clear_authority: true };
    else if (act === 'company') body = { company_name: e.target.value.trim() };
    if (!body) return;
    const r = await call(`/people/${id}`, 'PUT', body);
    if (r) { apply(r); say('Сохранено.'); }
  });
  $('pp-list').addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-act]'); const card = e.target.closest('[data-person]');
    if (!btn || !card) return;
    const id = card.dataset.person;
    if (btn.dataset.act === 'forget') {
      const name = state.people.find((p) => String(p.id) === id)?.display_name || '';
      if (!confirm(`Удалить «${name}» из проекта навсегда? Будут стёрты его написания, голосовые слепки проекта и данные в журнале изменений. Учётка в портале останется. Это нельзя отменить.`)) return;
      const r = await call(`/people/${id}`, 'DELETE');
      if (r) { apply(r); await reloadUnknown(); say('Человек удалён из проекта.'); }
    } else if (btn.dataset.act === 'link-voice') {
      const r = await call(`/people/${id}/voices/${btn.dataset.profile}/link`, 'POST');
      if (r) { apply(r); say('Голос привязан к учётке.'); }
    } else if (btn.dataset.act === 'consent') {
      const name = state.people.find((p) => String(p.id) === id)?.display_name || '';
      if (!confirm(`Вы получили согласие «${name}» на запись и распознавание голоса? Отзыв согласия потом делается только администратором в консоли.`)) return;
      const r = await call(`/people/${id}/voice-consent`, 'PUT', { given: true });
      if (r) { apply(r); say('Согласие отмечено.'); }
    } else if (btn.dataset.act === 'alias') {
      const input = card.querySelector('[data-act="alias-text"]'); const alias = input.value.trim();
      if (!alias) return;
      const r = await call(`/people/${id}/aliases`, 'POST', { alias });
      if (r) { apply(r); say('Написание добавлено.'); }
    } else if (btn.dataset.act === 'merge') {
      const drop = Number(card.querySelector('[data-act="merge-with"]').value);
      if (!drop) { say('Выберите, с кем объединить.', true); return; }
      const dropName = state.people.find((p) => p.id === drop)?.display_name || '';
      if (!confirm(`Объединить «${dropName}» с «${state.people.find((p) => String(p.id) === id).display_name}»? Их написания, голоса и участия станут одним человеком.`)) return;
      const r = await call(`/people/${id}/merge`, 'POST', { drop_id: drop });
      if (r) { apply(r); await reloadUnknown(); say('Объединено.'); }
    }
  });
  $('pp-unknown').addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-act]'); const card = e.target.closest('[data-unknown]');
    if (!btn || !card) return;
    const id = card.dataset.unknown; let body = null;
    if (btn.dataset.act === 'resolve') {
      const who = Number(card.querySelector('[data-act="who"]').value);
      if (!who) { say('Выберите человека.', true); return; }
      body = { person_id: who };
    } else if (btn.dataset.act === 'ignore') {
      body = { ignore: true };
    } else if (btn.dataset.act === 'new') {
      const label = state.unknown_items.find((u) => String(u.id) === id)?.label || '';
      const name = prompt('Имя нового человека:', label);
      if (!name || !name.trim()) return;
      const side = prompt('Сторона: заказчик, исполнитель или пусто, если неизвестно:', '');
      const s = (side || '').trim().toLowerCase();
      body = { new_name: name.trim(), side: s.startsWith('зак') ? 'customer' : (s.startsWith('исп') ? 'executor' : 'unknown') };
    }
    if (!body) return;
    const r = await call(`/people/unknown/${id}/resolve`, 'POST', body);
    if (r) { apply(r); await reloadUnknown(); say('Готово.'); }
  });

  $('pp-log-box').addEventListener('toggle', async (e) => {
    if (!e.target.open) return;
    const d = await call('/people/log?limit=30');
    if (!d) return;
    $('pp-log').innerHTML = d.items.map((r) => `<tr><td class="text-nowrap small">${esc((r.at || '').replace('T', ' ').slice(0, 16))}</td><td class="small">${esc(r.by || '—')}</td><td class="small">${esc(ACTION[r.action] || r.action)}</td></tr>`).join('') || '<tr><td class="small text-muted">Изменений пока нет.</td></tr>';
  });

  loadAll(); // сразу: значок «Кто это?» виден и у свёрнутой карточки
})();
