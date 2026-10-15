/* Сворачиваемые блоки с вкладками на странице проекта: «Агенты и MCP» (partials/agents_hub.html), «О проекте» (partials/project_hub.html).
   Разметка: карточка [data-hub="имя"], панель [data-hub-panel], подсказка [data-hub-hint], кнопки [data-hub-tab], вкладки [data-hub-pane].
   Открытость и вкладку помнит localStorage (<имя>_hub_open, <имя>_hub_tab). Когда вкладка становится видна, шлёт событие
   hub:show {hub, tab} — по нему px_agent.js лениво грузит данные. PageHubs.open(tab) раскрывает нужный блок на вкладке (переходы из «Что нового»). */
(function () {
  const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* без хранилища работает */ } } };
  const hubs = [];

  document.querySelectorAll('[data-hub]').forEach((card) => {
    const name = card.dataset.hub;
    const panel = card.querySelector('[data-hub-panel]');
    const buttons = Array.from(card.querySelectorAll('[data-hub-tab]'));
    if (!panel || !buttons.length) return;
    const hint = card.querySelector('[data-hub-hint]');
    const panes = Array.from(card.querySelectorAll('[data-hub-pane]'));
    const has = (tab) => buttons.some((b) => b.dataset.hubTab === tab);
    const saved = store.get(name + '_hub_tab');
    let current = has(saved) ? saved : buttons[0].dataset.hubTab;

    const announce = () => document.dispatchEvent(new CustomEvent('hub:show', { detail: { hub: name, tab: current } }));
    const show = () => { if (!panel.classList.contains('show')) bootstrap.Collapse.getOrCreateInstance(panel, { toggle: false }).show(); };

    function select(tab) {
      if (!has(tab)) return;
      current = tab;
      store.set(name + '_hub_tab', tab);
      buttons.forEach((b) => {
        const on = b.dataset.hubTab === tab;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      panes.forEach((p) => p.classList.toggle('d-none', p.dataset.hubPane !== tab));
      if (panel.classList.contains('show')) announce();
    }

    // на телефоне вкладки в одну строку с прокруткой: выбранную докручиваем в видимую часть
    buttons.forEach((b) => b.addEventListener('click', () => { select(b.dataset.hubTab); b.scrollIntoView({ block: 'nearest', inline: 'nearest' }); }));
    panel.addEventListener('shown.bs.collapse', (ev) => {
      if (ev.target !== panel) return;
      if (hint) hint.textContent = '(свернуть)';
      store.set(name + '_hub_open', '1');
      announce();
    });
    panel.addEventListener('hidden.bs.collapse', (ev) => {
      if (ev.target !== panel) return;
      if (hint) hint.textContent = '(развернуть)';
      store.set(name + '_hub_open', '0');
    });

    hubs.push({ has, select, show });
    select(current);
    if (store.get(name + '_hub_open') === '1') show();
  });

  window.PageHubs = {
    open(tab) {
      const hub = hubs.find((h) => h.has(tab));
      if (!hub) return false;
      hub.select(tab);
      hub.show();
      return true;
    },
  };
})();
