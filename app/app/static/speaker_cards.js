// Карточки голосов (/report-speaker-match): имя, компания, сторона и должность — сразу в «Люди и стороны».
// Данные страницы — #pp-cards (services/speaker_cards.page_context). Подсказка заполняет поля, но голос принимается
// только кнопкой «Подтвердить»; без компании и стороны кнопка неактивна.
(function () {
  const node = document.getElementById('pp-cards');
  if (!node) return;
  let ctx;
  try { ctx = JSON.parse(node.textContent); } catch (_) { return; }
  const SIDES = [['', 'Сторона…'], ['executor', 'Исполнитель'], ['customer', 'Заказчик']];

  // тот же ключ, что people.alias_key: без регистра, ё = е, знаки препинания — пробел
  const key = (s) => String(s || '').toLowerCase().replace(/ё/g, 'е').replace(/[^0-9a-zа-я]+/g, ' ').trim();

  const known = [];
  (ctx.people || []).forEach((p) => known.push({ ...p, source: 'Люди и стороны' }));
  (ctx.staff || []).forEach((p) => known.push({ ...p, source: 'карточка сотрудника' }));
  const findPerson = (name) => {
    const k = key(name);
    if (!k) return null;
    return known.find((p) => (p.keys || []).includes(k)) || null;
  };
  const companySide = (company) => (ctx.company_sides || {})[key(company)] || '';

  function datalist(id, values) {
    if (document.getElementById(id)) return;
    const dl = document.createElement('datalist');
    dl.id = id;
    values.forEach((v) => { const o = document.createElement('option'); o.value = v; dl.appendChild(o); });
    document.body.appendChild(dl);
  }
  datalist('pc-names', [...new Set(known.map((p) => p.name))]);
  datalist('pc-companies', ctx.companies || []);

  function field(labelText, control) {
    const wrap = document.createElement('div');
    wrap.className = 'pc-field';
    const label = document.createElement('label');
    label.className = 'form-label small fw-medium mb-1';
    label.textContent = labelText;
    label.htmlFor = control.id;
    wrap.append(label, control);
    return wrap;
  }

  function toast(text) {
    if (typeof window.showToast === 'function') window.showToast(text);
    else if (typeof showToast === 'function') showToast(text); // eslint-disable-line no-undef
    else alert(text);
  }

  document.querySelectorAll('.speaker-card').forEach((card) => {
    const label = card.dataset.speaker;
    const nameInp = card.querySelector('.name-inp');
    const oldBtn = card.querySelector('.enroll');
    if (!label || !nameInp || !oldBtn) return;
    const info = (ctx.cards || {})[label] || {};
    const confirmed = info.status === 'confirmed' && !info.needs_human;
    const slug = label.replace(/[^a-z0-9]+/gi, '-');

    const company = document.createElement('input');
    company.id = `pc-company-${slug}`;
    company.className = 'form-control form-control-sm pc-company';
    company.setAttribute('list', 'pc-companies');
    company.placeholder = 'Выберите из списка';
    company.autocomplete = 'off';
    const side = document.createElement('select');
    side.id = `pc-side-${slug}`;
    side.className = 'form-select form-select-sm pc-side';
    SIDES.forEach(([v, t]) => { const o = document.createElement('option'); o.value = v; o.textContent = t; side.appendChild(o); });
    const job = document.createElement('input');
    job.id = `pc-job-${slug}`;
    job.className = 'form-control form-control-sm pc-job';
    job.placeholder = 'Необязательно';
    job.autocomplete = 'off';
    const note = document.createElement('p');
    note.className = 'pc-note small text-muted mb-0';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-sm btn-soft-accent pc-confirm';
    btn.textContent = confirmed ? 'Обновить' : 'Подтвердить';

    nameInp.setAttribute('list', 'pc-names');
    nameInp.autocomplete = 'off';
    const group = oldBtn.closest('.input-group');
    oldBtn.remove(); // старая кнопка писала только имя голоса
    const grid = document.createElement('div');
    grid.className = 'pc-grid';
    grid.append(field('Компания', company), field('Сторона', side));
    const block = document.createElement('div');
    block.className = 'pc-block';
    block.append(grid, field('Должность', job), note, btn);
    (group || nameInp).after(block);

    // что ввёл человек — не перетираем подсказкой
    const touched = { company: false, side: false, job: false };
    company.addEventListener('input', () => { touched.company = true; });
    side.addEventListener('change', () => { touched.side = true; });
    job.addEventListener('input', () => { touched.job = true; });

    function fill(person) {
      if (!person) { note.textContent = ''; return; }
      if (!touched.company && person.company) company.value = person.company;
      if (!touched.side && person.side) side.value = person.side;
      if (!touched.job && person.job_title) job.value = person.job_title;
      note.textContent = `Заполнено по данным: ${person.source}. Проверьте перед подтверждением.`;
    }
    function sync() {
      const ok = nameInp.value.trim() && company.value.trim() && side.value;
      btn.disabled = !ok;
      btn.title = ok ? '' : 'Укажите имя, компанию и сторону';
    }

    fill(findPerson(nameInp.value) || (info.suggestion ? { ...info.suggestion } : null));
    nameInp.addEventListener('input', () => { fill(findPerson(nameInp.value)); sync(); });
    company.addEventListener('input', () => {
      const s = companySide(company.value);
      if (s && !touched.side) side.value = s;
      sync();
    });
    side.addEventListener('change', sync);
    sync();

    btn.addEventListener('click', async () => {
      const body = {
        diar_label: label,
        display_name: nameInp.value.trim(),
        company: company.value.trim(),
        side: side.value,
        job_title: job.value.trim() || null,
      };
      btn.disabled = true;
      const prev = btn.textContent;
      btn.textContent = '…';
      try {
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
        toast('Сохранено: ' + body.display_name);
        location.reload();
      } catch (err) {
        toast('Ошибка: ' + err.message);
        btn.textContent = prev;
        sync();
      }
    });
  });
})();
