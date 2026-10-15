// Карточки голосов (/report-speaker-match): «Кто это» — выбор из списка, затем компания, сторона, должность.
// Данные страницы — #pp-cards (services/speaker_cards.page_context). Список: похожие голоса базы (с % сходства), люди
// проекта, сотрудники; «Новый человек» — только если нужного нет. Выбор привязывает голос к той самой записи, а не к
// написанию. Подсказка заполняет поля, но голос принимается только кнопкой «Подтвердить»; без компании и стороны она
// неактивна. Исправить написание голоса во всех созвонах — отдельная ссылка с подтверждением.
(function () {
  const node = document.getElementById('pp-cards');
  if (!node) return;
  let ctx;
  try { ctx = JSON.parse(node.textContent); } catch (_) { return; }
  const SIDES = [['', 'Сторона…'], ['executor', 'Исполнитель'], ['customer', 'Заказчик']];
  const SIDE_LABEL = { executor: 'исполнитель', customer: 'заказчик' };

  // тот же ключ, что people.alias_key: без регистра, ё = е, знаки препинания — пробел
  const key = (s) => String(s || '').toLowerCase().replace(/ё/g, 'е').replace(/[^0-9a-zа-я]+/g, ' ').trim();
  const companySide = (company) => (ctx.company_sides || {})[key(company)] || '';

  const peopleOpts = (ctx.people || []).map((p) => ({
    kind: 'person', name: p.name, keys: p.keys || [key(p.name)], person_id: p.id, profile_id: p.profile_id || null,
    info: { ...p, source: 'Люди и стороны' },
  }));
  const peopleKeys = new Set(peopleOpts.flatMap((o) => o.keys));
  const staffOpts = (ctx.staff || [])
    .filter((s) => !(s.keys || []).some((k) => peopleKeys.has(k)))
    .map((s) => ({ kind: 'staff', name: s.name, keys: s.keys || [key(s.name)], info: { ...s, source: 'карточка сотрудника' } }));

  function toast(text) {
    if (typeof window.showToast === 'function') window.showToast(text);
    else alert(text);
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function field(labelText, control) {
    const wrap = el('div', 'pc-field');
    const label = el('label', 'form-label small fw-medium mb-1', labelText);
    label.htmlFor = control.id;
    wrap.append(label, control);
    return wrap;
  }

  function describe(info) {
    if (!info) return '';
    return [info.company, SIDE_LABEL[info.side]].filter(Boolean).join(' · ');
  }

  document.addEventListener('click', (e) => {
    document.querySelectorAll('.pc-menu.show').forEach((m) => {
      if (!m.parentElement.contains(e.target)) m.classList.remove('show');
    });
  });

  document.querySelectorAll('.speaker-card').forEach((card) => {
    const label = card.dataset.speaker;
    const nameInp = card.querySelector('.name-inp');
    const oldBtn = card.querySelector('.enroll');
    if (!label || !nameInp || !oldBtn) return;
    const info = (ctx.cards || {})[label] || {};
    const current = info.current;
    const confirmed = info.status === 'confirmed' && !info.needs_human;
    const slug = label.replace(/[^a-z0-9]+/gi, '-');
    const voiceOpts = (info.candidates || []).map((c) => ({
      kind: 'voice', name: c.name, keys: [key(c.name)], profile_id: c.profile_id, pct: c.pct,
      person_id: c.info && c.info.id ? c.info.id : null, info: c.info,
    }));

    // поле «Кто это»: поиск по списку, свой выпадающий список вместо datalist
    const nameLabel = card.querySelector(`label[for="${nameInp.id}"]`);
    if (nameLabel) nameLabel.textContent = 'Кто это';
    nameInp.removeAttribute('list');
    nameInp.autocomplete = 'off';
    nameInp.placeholder = 'Выберите из списка или введите имя';
    nameInp.setAttribute('role', 'combobox');
    nameInp.setAttribute('aria-expanded', 'false');
    const group = oldBtn.closest('.input-group');
    oldBtn.remove(); // старая кнопка писала только имя голоса
    const anchor = group || nameInp;
    const wrap = el('div', 'pc-who position-relative');
    anchor.before(wrap);
    wrap.append(anchor);
    const menu = el('div', 'dropdown-menu pc-menu');
    menu.id = `pc-menu-${slug}`;
    menu.setAttribute('role', 'listbox');
    nameInp.setAttribute('aria-controls', menu.id);
    wrap.append(menu);
    const chip = el('p', 'pc-chip small mb-0');

    const company = el('input', 'form-control form-control-sm pc-company');
    company.id = `pc-company-${slug}`;
    company.setAttribute('list', 'pc-companies');
    company.placeholder = 'Выберите из списка';
    company.autocomplete = 'off';
    const side = el('select', 'form-select form-select-sm pc-side');
    side.id = `pc-side-${slug}`;
    SIDES.forEach(([v, t]) => { const o = el('option', '', t); o.value = v; side.appendChild(o); });
    const job = el('input', 'form-control form-control-sm pc-job');
    job.id = `pc-job-${slug}`;
    job.placeholder = 'Необязательно';
    job.autocomplete = 'off';
    const note = el('p', 'pc-note small text-muted mb-0');
    const btn = el('button', 'btn btn-sm btn-soft-accent pc-confirm', confirmed ? 'Обновить' : 'Подтвердить');
    btn.type = 'button';
    const grid = el('div', 'pc-grid');
    grid.append(field('Компания', company), field('Сторона', side));
    const block = el('div', 'pc-block');
    block.append(chip, grid, field('Должность', job), note, btn);
    if (current) {
      const fix = el('button', 'btn btn-link btn-sm p-0 pc-rename', 'Исправить написание имени');
      fix.type = 'button';
      fix.addEventListener('click', () => renameVoice());
      block.append(fix);
    }
    wrap.after(block);

    let choice = null;
    const touched = { company: false, side: false, job: false };
    company.addEventListener('input', () => { touched.company = true; });
    side.addEventListener('change', () => { touched.side = true; });
    job.addEventListener('input', () => { touched.job = true; });

    function fill(src, force) {
      if (!src) { note.textContent = ''; return; }
      if ((force || !touched.company) && src.company) { company.value = src.company; touched.company = false; }
      if ((force || !touched.side) && src.side) { side.value = src.side; touched.side = false; }
      if ((force || !touched.job) && src.job_title) { job.value = src.job_title; touched.job = false; }
      if (!side.value && company.value) side.value = companySide(company.value);
      note.textContent = src.source ? `Заполнено по данным: ${src.source}. Проверьте перед подтверждением.` : '';
    }

    function showChip() {
      chip.textContent = '';
      chip.className = 'pc-chip small mb-0';
      if (!choice) {
        if (nameInp.value.trim()) {
          chip.textContent = 'Нет в списке — будет заведён новый человек';
          chip.classList.add('pc-chip-new');
        }
        return;
      }
      const text = {
        current: 'Голос этой карточки',
        voice: `Голос из базы · сходство ${choice.pct}%`,
        person: choice.profile_id ? 'Человек проекта · голос уже есть' : 'Человек проекта · голоса ещё нет',
        staff: 'Сотрудник · из карточки',
      }[choice.kind] || '';
      chip.textContent = text;
      chip.classList.add('pc-chip-known');
    }

    function sync() {
      const ok = nameInp.value.trim() && company.value.trim() && side.value;
      btn.disabled = !ok;
      btn.title = ok ? '' : 'Укажите, кто это, компанию и сторону';
    }

    function choose(opt) {
      choice = opt;
      nameInp.value = opt.name;
      menu.classList.remove('show');
      nameInp.setAttribute('aria-expanded', 'false');
      fill(opt.info ? { ...opt.info, source: opt.info.source } : null, true);
      showChip();
      sync();
    }

    function option(opt, extra) {
      const item = el('button', 'dropdown-item pc-opt');
      item.type = 'button';
      item.setAttribute('role', 'option');
      item.append(el('span', 'pc-opt-name', opt.name));
      const meta = [extra, describe(opt.info)].filter(Boolean).join(' · ');
      if (meta) item.append(el('span', 'pc-opt-meta small text-muted', meta));
      item.addEventListener('click', () => choose(opt));
      return item;
    }

    function renderMenu() {
      menu.textContent = '';
      const q = key(nameInp.value);
      const fits = (o) => !q || (choice && key(choice.name) === q) || o.keys.some((k) => k.includes(q));
      const sections = [
        ['Похожие голоса', voiceOpts.filter(fits), (o) => `${o.pct}%`],
        ['Люди проекта', peopleOpts.filter(fits), (o) => (o.profile_id ? 'есть голос' : '')],
        ['Сотрудники', staffOpts.filter(fits), () => ''],
      ];
      let any = false;
      sections.forEach(([title, opts, extra]) => {
        if (!opts.length) return;
        any = true;
        menu.append(el('h6', 'dropdown-header', title));
        opts.slice(0, 30).forEach((o) => menu.append(option(o, extra(o))));
      });
      const typed = nameInp.value.trim();
      const exact = [...voiceOpts, ...peopleOpts, ...staffOpts].some((o) => o.keys.includes(q));
      if (typed && !exact) {
        if (any) menu.append(el('div', 'dropdown-divider'));
        const item = el('button', 'dropdown-item pc-opt pc-opt-new');
        item.type = 'button';
        item.append(el('span', 'pc-opt-name', `+ Новый человек: «${typed}»`));
        item.addEventListener('click', () => {
          choice = null;
          menu.classList.remove('show');
          showChip();
          company.focus();
        });
        menu.append(item);
        any = true;
      }
      menu.classList.toggle('show', any);
      nameInp.setAttribute('aria-expanded', any ? 'true' : 'false');
    }

    nameInp.addEventListener('focus', renderMenu);
    nameInp.addEventListener('input', () => {
      if (choice && key(choice.name) !== key(nameInp.value)) {
        choice = null;
        note.textContent = '';
      }
      renderMenu();
      showChip();
      sync();
    });
    nameInp.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') menu.classList.remove('show');
      if (e.key === 'ArrowDown') { const first = menu.querySelector('.pc-opt'); if (first) { e.preventDefault(); first.focus(); } }
    });
    menu.addEventListener('keydown', (e) => {
      const items = [...menu.querySelectorAll('.pc-opt')];
      const i = items.indexOf(document.activeElement);
      if (e.key === 'ArrowDown' && i < items.length - 1) { e.preventDefault(); items[i + 1].focus(); }
      if (e.key === 'ArrowUp') { e.preventDefault(); (i > 0 ? items[i - 1] : nameInp).focus(); }
      if (e.key === 'Escape') { menu.classList.remove('show'); nameInp.focus(); }
    });
    company.addEventListener('input', () => {
      const s = companySide(company.value);
      if (s && !touched.side) side.value = s;
      sync();
    });
    side.addEventListener('change', sync);

    // начальное состояние: голос карточки, иначе вариант с тем же именем, что подставила страница
    const startKey = key(nameInp.value);
    if (current && key(current.name) === startKey) {
      const twin = [...peopleOpts, ...voiceOpts].find((o) => o.keys.includes(startKey));
      choice = { kind: 'current', name: current.name, profile_id: current.profile_id, person_id: twin ? twin.person_id : null };
      fill(info.suggestion || (twin && twin.info), false);
    } else if (startKey) {
      const twin = [...voiceOpts, ...peopleOpts, ...staffOpts].find((o) => o.keys.includes(startKey));
      if (twin) { choice = twin; fill(twin.info, false); }
    }
    showChip();
    sync();

    function known(name) {
      const k = key(name);
      return [...voiceOpts, ...peopleOpts, ...staffOpts].find((o) => o.keys.includes(k)) || null;
    }
    function similar(name) {
      const first = key(name).split(' ')[0];
      if (!first || first.length < 3) return null;
      return [...voiceOpts, ...peopleOpts, ...staffOpts].find((o) => o.keys.some((k) => k.split(' ').includes(first))) || null;
    }

    async function send(body) {
      const res = await fetch(`/api/artifacts/${encodeURIComponent(ctx.artifact_id)}/speaker-card`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json;charset=utf-8' },
        body: JSON.stringify(body),
      });
      const text = await res.text();
      if (!res.ok) {
        let detail = text;
        try { detail = JSON.parse(text).detail || text; } catch (_) { /* текст как есть */ }
        throw new Error(typeof detail === 'string' ? detail : 'проверьте поля');
      }
    }

    function baseBody(name) {
      return { diar_label: label, display_name: name, company: company.value.trim(), side: side.value, job_title: job.value.trim() || null };
    }

    async function run(body, done) {
      btn.disabled = true;
      const prev = btn.textContent;
      btn.textContent = '…';
      try {
        await send(body);
        toast(done);
        location.reload();
      } catch (err) {
        toast('Ошибка: ' + err.message);
        btn.textContent = prev;
        sync();
      }
    }

    btn.addEventListener('click', () => {
      const typed = nameInp.value.trim();
      let pick = choice || known(typed);
      const body = baseBody(typed);
      if (!pick) {
        const like = similar(typed);
        if (like && !confirm(`Похоже на «${like.name}». Это другой человек?\n\nОК — завести нового «${typed}», Отмена — выбрать из списка.`)) {
          nameInp.focus();
          renderMenu();
          return;
        }
        body.new_person = true;
      } else {
        if (pick.profile_id) body.profile_id = pick.profile_id;
        if (pick.person_id) body.person_id = pick.person_id;
        body.display_name = pick.name;
      }
      run(body, 'Сохранено: ' + body.display_name);
    });

    async function renameVoice() {
      if (!current) return;
      const next = (prompt(`Новое написание имени для «${current.name}» (изменится во всех созвонах этого голоса):`, current.name) || '').trim();
      if (!next || key(next) === key(current.name)) return;
      if (!company.value.trim() || !side.value) { toast('Сначала укажите компанию и сторону'); return; }
      if (!confirm(`Переименовать «${current.name}» в «${next}» во всех созвонах?`)) return;
      run({ ...baseBody(next), profile_id: current.profile_id, rename: true }, 'Переименовано: ' + next);
    }
  });

  // список компаний для поля «Компания»
  if (!document.getElementById('pc-companies')) {
    const dl = document.createElement('datalist');
    dl.id = 'pc-companies';
    (ctx.companies || []).forEach((v) => { const o = document.createElement('option'); o.value = v; dl.appendChild(o); });
    document.body.appendChild(dl);
  }
})();
