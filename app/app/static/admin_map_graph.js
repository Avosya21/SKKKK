// Страница «Граф зависимостей» (/admin/map/graph): Cytoscape-схема узлов и связей проекта.
// Данные — /admin/map/graph/summary (цифры, легенда, состояние индекса) и /admin/map/graph/data
// (модульный свод по файлам, окрестность узла или подсистема). Только для роли admin.
// Цвета берутся из токенов дизайн-системы (--ds-*) в момент отрисовки — своих цветов здесь нет.
(() => {
  'use strict';
  if (!SKKKK.requireAuth()) return;

  const KIND_ORDER = ['symbol', 'route', 'ui', 'table', 'column', 'env', 'file', 'manifest', 'commit', 'external'];
  const KIND_TOKEN = {
    symbol: '--ds-info', route: '--ds-ok', table: '--ds-warn', column: '--ds-warn',
    env: '--ds-text-2', ui: '--ds-link', external: '--ds-muted', commit: '--ds-accent', manifest: '--ds-line-strong',
  };
  const PROPS = [
    ['sub', 'Подпись'], ['sig', 'Сигнатура'], ['handler', 'Обработчик'], ['documented', 'Описание'],
    ['default', 'Значение по умолчанию'], ['tags', 'Метки'], ['cols', 'Столбцы'],
    ['writers', 'Кто пишет'], ['readers', 'Кто читает'], ['guards', 'Проверки прав'],
    ['deps', 'Зависимости'], ['endpoints', 'Связанные адреса'],
  ];
  const LIMIT_MAX = 1500;
  const LABELS = readLabels();

  const state = {
    view: 'modules', focus: '', group: '', depth: 2, direction: 'both', limit: 600,
    nodes: [], edges: [], center: null, selected: null, cy: null, summaryNotes: [],
    byKind: {}, byRelation: {}, checkedKinds: new Set(), checkedRelations: new Set(), allKinds: [], allRelations: [],
  };

  const dom = {
    form: null, q: null, search: null, viewModules: null, viewEntities: null,
    group: null, depth: null, direction: null, reset: null, more: null, hint: null,
    kinds: null, relations: null, relationsHint: null, kindsHint: null,
    counters: null, truncated: null, stale: null, source: null, notes: null,
    canvas: null, node: null, nodeTitle: null, nodeKind: null, nodeClose: null,
    nodeWhere: null, nodeProps: null, nodeIn: null, nodeOut: null,
  };

  // ───────────────────────── утилиты ─────────────────────────

  function readLabels() {
    const holder = document.getElementById('amg-labels');
    if (!holder) return { kinds: {}, relations: {}, subsystems: [] };
    try {
      const parsed = JSON.parse(holder.textContent || '{}');
      return { kinds: parsed.kinds || {}, relations: parsed.relations || {}, subsystems: parsed.subsystems || [] };
    } catch {
      return { kinds: {}, relations: {}, subsystems: [] };
    }
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function cssToken(name) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (value || '').trim() || 'gray';
  }

  function kindLabel(kind) {
    return LABELS.kinds[kind] || kind || 'узел';
  }

  function relLabel(rel) {
    return LABELS.relations[rel] || String(rel || '').replace(/^m:/, 'карта: ');
  }

  /** Значение из ответа сервера — строкой (массивы и флаги приводятся к читаемому виду). */
  function asText(value) {
    if (value === null || value === undefined || value === '') return '';
    if (Array.isArray(value)) return value.map((item) => asText(item)).filter(Boolean).join(', ');
    if (typeof value === 'boolean') return value ? 'есть' : 'нет';
    if (typeof value === 'object') return Object.entries(value).map(([k, v]) => `${k}: ${asText(v)}`).join('; ');
    return String(value);
  }

  function fmtWhen(value) {
    if (!value) return '';
    const numeric = Number(value);
    const date = Number.isFinite(numeric) && String(value).length <= 12 ? new Date(numeric * 1000) : new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' });
  }

  function showNotes(messages) {
    dom.notes.replaceChildren();
    for (const message of messages || []) {
      if (!message) continue;
      dom.notes.appendChild(el('div', 'alert alert-warning py-2 mb-0 small', message));
    }
  }

  function adminOnly(message) {
    dom.notes.replaceChildren();
    dom.notes.appendChild(el('div', 'alert alert-danger py-2 mb-0 small', message || 'Нужна роль администратора.'));
  }

  // ───────────────────────── запросы ─────────────────────────

  async function loadSummary() {
    const response = await SKKKK.api('/admin/map/graph/summary');
    if (response.status === 403) {
      adminOnly('Нужна роль администратора.');
      return false;
    }
    if (!response.ok) {
      adminOnly('Не удалось получить сводку графа.');
      return false;
    }
    const data = await response.json();
    const stats = data.stats || {};
    state.byKind = stats.by_kind || {};
    state.byRelation = stats.by_relation || {};
    renderKinds();
    renderRelations();
    renderSubsystems(data.groups, stats.by_group);
    renderStatus(data);
    state.summaryNotes = data.notes || [];
    showNotes(state.summaryNotes);
    return true;
  }

  async function loadData() {
    if (state.view === 'entities' && !state.focus && !state.group) {
      state.nodes = [];
      state.edges = [];
      drawGraph();
      dom.counters.textContent = 'Выберите узел или подсистему';
      dom.truncated.classList.add('d-none');
      dom.more.disabled = true;
      showNotes(state.summaryNotes);
      clearNode();
      return;
    }
    const response = await SKKKK.api(`/admin/map/graph/data?${buildParams().toString()}`);
    if (response.status === 403) {
      adminOnly('Нужна роль администратора.');
      return;
    }
    if (!response.ok) {
      adminOnly('Не удалось получить граф.');
      return;
    }
    const payload = await response.json();
    state.nodes = payload.nodes || [];
    state.edges = payload.edges || [];
    state.center = payload.center || null;
    drawGraph();
    renderCounters(payload);
    showNotes([...state.summaryNotes, ...(payload.notes || [])]);
    if (payload.truncated) {
      dom.truncated.textContent = `показаны не все узлы (всего ${payload.total_nodes})`;
      dom.truncated.classList.remove('d-none');
    } else {
      dom.truncated.classList.add('d-none');
    }
    if (state.center) selectNode(state.center.id);
    else clearNode();
  }

  function buildParams() {
    const params = new URLSearchParams();
    params.set('limit', String(state.limit));
    params.set('view', state.view);
    if (state.view === 'entities') {
      if (state.focus) {
        params.set('focus', state.focus);
        params.set('depth', String(state.depth));
        params.set('direction', state.direction);
      } else if (state.group) {
        params.set('group', state.group);
      }
      const relations = checkedParam(state.checkedRelations, state.allRelations);
      if (relations) params.set('relations', relations);
    }
    const kinds = checkedParam(state.checkedKinds, state.allKinds);
    if (kinds) params.set('kinds', kinds);
    return params;
  }

  function checkedParam(checked, all) {
    if (!all.length || checked.size === all.length) return '';
    return all.filter((item) => checked.has(item)).join(',');
  }

  // ───────────────────────── легенда и фильтры ─────────────────────────

  function sortKinds(names) {
    return names.slice().sort((a, b) => {
      const orderA = KIND_ORDER.indexOf(a);
      const orderB = KIND_ORDER.indexOf(b);
      if (orderA !== orderB) return (orderA < 0 ? 99 : orderA) - (orderB < 0 ? 99 : orderB);
      return (state.byKind[b] || 0) - (state.byKind[a] || 0);
    });
  }

  function renderKinds() {
    const names = sortKinds(Object.keys(state.byKind));
    state.allKinds = names;
    if (!names.length) {
      dom.kinds.replaceChildren(el('div', 'amg-empty', 'Вид узлов появится после сборки графа.'));
      return;
    }
    if (!state.checkedKinds.size) names.forEach((name) => state.checkedKinds.add(name));
    dom.kinds.replaceChildren(...names.map((name) => legendRow(name, state.byKind[name], state.checkedKinds, renderKinds, true)));
    updateFiltersHint();
  }

  function renderRelations() {
    const names = Object.keys(state.byRelation).sort((a, b) => (state.byRelation[b] || 0) - (state.byRelation[a] || 0));
    state.allRelations = names;
    if (!names.length) {
      dom.relations.replaceChildren(el('div', 'amg-empty', 'Связи появятся после сборки графа.'));
      return;
    }
    if (!state.checkedRelations.size) names.forEach((name) => state.checkedRelations.add(name));
    dom.relations.replaceChildren(...names.map((name) => legendRow(name, state.byRelation[name], state.checkedRelations, renderRelations, false)));
    updateFiltersHint();
  }

  function legendRow(name, count, checked, redraw, isKind) {
    const row = el('label', 'form-check mb-1');
    const box = el('input', 'form-check-input');
    box.type = 'checkbox';
    box.checked = checked.has(name);
    box.addEventListener('change', () => {
      if (box.checked) checked.add(name);
      else checked.delete(name);
      redraw();
      loadData();
    });
    row.appendChild(box);
    if (isKind) {
      const swatch = el('span', 'amg-swatch');
      const token = KIND_TOKEN[name];
      if (token) swatch.style.background = cssToken(token);
      row.appendChild(swatch);
    }
    row.appendChild(el('span', 'text-break', isKind ? kindLabel(name) : relLabel(name)));
    row.appendChild(el('span', 'amg-count', count === undefined || count === null ? '' : count));
    return row;
  }

  function renderSubsystems(groups, byGroup) {
    const names = (groups && groups.length ? groups : Object.keys(byGroup || {}));
    const keep = dom.group.value;
    dom.group.replaceChildren(el('option', null, 'Все подсистемы (в режиме «Сущности»)'));
    for (const name of names) {
      const option = el('option', null, name);
      option.value = name;
      dom.group.appendChild(option);
    }
    if (keep && names.includes(keep)) dom.group.value = keep;
  }

  function renderStatus(summary) {
    const stale = summary.stale || {};
    const parts = [];
    if (stale.changed) parts.push(`изменено файлов: ${stale.changed}`);
    if (stale.removed) parts.push(`удалено: ${stale.removed}`);
    if (stale.no_vector) parts.push(`без вектора: ${stale.no_vector}`);
    if (parts.length) {
      dom.stale.textContent = `индекс отстаёт — ${parts.join(', ')}`;
      dom.stale.title = 'Обновите индекс: venv\\Scripts\\python.exe scripts\\index_project_map.py';
      dom.stale.classList.remove('d-none');
    } else {
      dom.stale.classList.add('d-none');
    }
    const source = summary.source || {};
    const when = fmtWhen(source.indexed_at);
    const head = source.git_head ? ` · коммит ${String(source.git_head).slice(0, 7)}` : '';
    const hours = stale.age_hours === null || stale.age_hours === undefined ? '' : ` · индекс ${Math.round(stale.age_hours)} ч назад`;
    dom.source.textContent = source.db ? `источник: ${source.db}${when ? `, собран ${when}` : ''}${head}${hours}` : '';
  }

  function renderCounters(payload) {
    const nodes = state.nodes.length;
    const edges = state.edges.length;
    dom.counters.textContent = `${SKKKK.count(nodes, ['узел', 'узла', 'узлов'])} · ${SKKKK.count(edges, ['связь', 'связи', 'связей'])}` +
      (payload.total_nodes ? ` (всего ${payload.total_nodes} / ${payload.total_edges})` : '');
    dom.more.disabled = state.limit >= LIMIT_MAX || !payload.truncated;
  }

  // ───────────────────────── граф ─────────────────────────

  function degreeMap() {
    const degrees = Object.create(null);
    for (const edge of state.edges) {
      const weight = edge.n || 1;
      degrees[edge.from] = (degrees[edge.from] || 0) + weight;
      degrees[edge.to] = (degrees[edge.to] || 0) + weight;
    }
    return degrees;
  }

  function graphStyle() {
    const colors = {
      text: cssToken('--ds-text-2'), surface: cssToken('--ds-surface'), line: cssToken('--ds-line-strong'),
      accent: cssToken('--ds-accent'), muted: cssToken('--ds-muted'),
    };
    const style = [
      {
        selector: 'node',
        style: {
          label: 'data(label)', 'font-size': 10, color: colors.text, 'text-wrap': 'wrap', 'text-max-width': '150px',
          'text-valign': 'bottom', 'text-halign': 'center', 'text-margin-y': 3,
          'text-outline-color': colors.surface, 'text-outline-width': 2,
          'background-color': colors.surface, 'border-width': 1, 'border-color': colors.line,
          width: 'data(size)', height: 'data(size)',
        },
      },
      {
        selector: 'edge',
        style: {
          width: 'data(width)', 'line-color': colors.line, 'target-arrow-color': colors.line, 'target-arrow-shape': 'triangle',
          'arrow-scale': 0.7, 'curve-style': 'bezier', opacity: 0.7,
        },
      },
      { selector: 'node:selected', style: { 'border-width': 3, 'border-color': colors.accent } },
      { selector: 'edge:selected', style: { 'line-color': colors.accent, 'target-arrow-color': colors.accent, opacity: 1 } },
      { selector: '.amg-dim', style: { opacity: 0.15 } },
    ];
    for (const [kind, token] of Object.entries(KIND_TOKEN)) {
      const color = cssToken(token);
      style.push({ selector: `node[kind = "${kind}"]`, style: { 'background-color': color, 'border-color': color } });
    }
    return style;
  }

  function drawGraph() {
    const degrees = degreeMap();
    const ids = new Set(state.nodes.map((node) => node.id));
    const elements = [];
    for (const node of state.nodes) {
      const degree = degrees[node.id] || 0;
      elements.push({
        data: {
          id: node.id, label: node.label || node.id, kind: node.kind || '', path: node.path || '', line: node.line || 0,
          group: node.group || '', size: 14 + Math.min(18, Math.round(Math.sqrt(degree) * 4)),
        },
      });
    }
    const seen = new Set();
    for (const edge of state.edges) {
      if (!ids.has(edge.from) || !ids.has(edge.to)) continue;
      const id = `e\u0000${edge.from}\u0000${edge.to}\u0000${edge.rel}`;
      if (seen.has(id)) continue;
      seen.add(id);
      const weight = edge.n || 1;
      elements.push({
        data: {
          id, source: edge.from, target: edge.to, rel: edge.rel,
          relLabel: relLabel(edge.rel), n: weight, width: 0.6 + Math.min(4, Math.log2(1 + weight)),
        },
      });
    }

    if (state.cy) {
      state.cy.destroy();
      state.cy = null;
    }
    const layout = state.nodes.length > 250
      ? { name: 'concentric', animate: false, minNodeSpacing: 10, levelWidth: () => 2, concentric: (node) => node.degree() }
      : { name: 'cose', animate: false, nodeRepulsion: 9000, idealEdgeLength: 90, numIter: 600 };
    state.cy = cytoscape({
      container: dom.canvas,
      elements,
      style: graphStyle(),
      layout,
      wheelSensitivity: 0.2,
      minZoom: 0.05,
      maxZoom: 4,
    });
    state.cy.on('tap', 'node', (event) => selectNode(event.target.id()));
    state.cy.on('tap', (event) => { if (event.target === state.cy) clearNode(); });
    if (state.nodes.length) state.cy.fit(undefined, 24);
    if (!state.nodes.length) {
      dom.canvas.setAttribute('aria-label', 'Граф пуст: под фильтры не попал ни один узел');
    } else {
      dom.canvas.setAttribute('aria-label', `Граф зависимостей: ${state.nodes.length} узлов`);
    }
  }

  // ───────────────────────── панель узла ─────────────────────────

  function nodeById(id) {
    return state.nodes.find((node) => node.id === id) || null;
  }

  function clearNode() {
    state.selected = null;
    dom.node.classList.add('d-none');
    if (state.cy) {
      state.cy.elements().removeClass('amg-dim');
      state.cy.elements().unselect();
    }
  }

  function selectNode(id) {
    const node = nodeById(id);
    if (!node) {
      clearNode();
      return;
    }
    state.selected = id;
    dom.node.classList.remove('d-none');
    dom.nodeTitle.textContent = node.label || node.id;
    dom.nodeKind.textContent = kindLabel(node.kind);
    renderWhere(node);
    renderProps(node);
    renderEdges(node);
    if (state.cy) {
      const neighbours = new Set([id]);
      for (const edge of state.edges) {
        if (edge.from === id) neighbours.add(edge.to);
        if (edge.to === id) neighbours.add(edge.from);
      }
      state.cy.elements().removeClass('amg-dim');
      state.cy.nodes().forEach((item) => { if (!neighbours.has(item.id())) item.addClass('amg-dim'); });
      state.cy.edges().forEach((item) => {
        if (item.data('source') !== id && item.data('target') !== id) item.addClass('amg-dim');
      });
      state.cy.elements().unselect();
      state.cy.getElementById(id).select();
    }
  }

  function renderWhere(node) {
    dom.nodeWhere.replaceChildren();
    if (node.path) {
      dom.nodeWhere.appendChild(el('code', 'small text-break', node.line ? `${node.path}:${node.line}` : node.path));
    }
    if (node.group) dom.nodeWhere.appendChild(el('span', 'amg-hint ms-2', node.group));
    if (node.ide) {
      const link = el('a', 'small ms-2', 'открыть в PyCharm');
      link.href = node.ide;
      link.rel = 'noopener';
      dom.nodeWhere.appendChild(link);
    }
  }

  function renderProps(node) {
    dom.nodeProps.replaceChildren();
    for (const [field, caption] of PROPS) {
      const value = asText(node[field]);
      if (!value) continue;
      dom.nodeProps.appendChild(el('dt', 'col-12 col-sm-4 col-lg-3', caption));
      dom.nodeProps.appendChild(el('dd', 'col-12 col-sm-8 col-lg-9 mb-1 text-break', value));
    }
    if (!dom.nodeProps.childElementCount) {
      dom.nodeProps.appendChild(el('dd', 'col-12 amg-empty mb-0', 'Дополнительных сведений нет.'));
    }
  }

  function renderEdges(node) {
    const incoming = [];
    const outgoing = [];
    for (const edge of state.edges) {
      if (edge.to === node.id) incoming.push(edge);
      if (edge.from === node.id) outgoing.push(edge);
    }
    dom.nodeIn.replaceChildren(...edgeRows(incoming, 'from'));
    dom.nodeOut.replaceChildren(...edgeRows(outgoing, 'to'));
    if (!dom.nodeIn.childElementCount) dom.nodeIn.appendChild(el('div', 'list-group-item amg-empty', 'Входящих связей нет.'));
    if (!dom.nodeOut.childElementCount) dom.nodeOut.appendChild(el('div', 'list-group-item amg-empty', 'Исходящих связей нет.'));
  }

  function edgeRows(edges, side) {
    const rows = [];
    const seen = new Set();
    for (const edge of edges) {
      const otherId = side === 'from' ? edge.from : edge.to;
      const key = `${edge.rel}\u0000${otherId}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const other = nodeById(otherId);
      const button = el('button', 'list-group-item list-group-item-action text-break');
      button.type = 'button';
      button.appendChild(el('span', 'fw-medium', relLabel(edge.rel)));
      button.appendChild(el('span', null, ' · '));
      button.appendChild(el('span', null, other ? (other.label || otherId) : otherId));
      if (edge.line) button.appendChild(el('span', 'amg-hint ms-1', `строка ${edge.line}`));
      if (other) {
        button.addEventListener('click', () => {
          state.view = 'entities';
          dom.viewEntities.checked = true;
          dom.q.value = other.label || other.id;
          setView('entities');
          state.focus = other.id;
          state.group = '';
          dom.group.value = '';
          loadData();
        });
      }
      rows.push(button);
    }
    return rows;
  }

  // ───────────────────────── управление ─────────────────────────

  function setView(view) {
    state.view = view;
    const isEntities = view === 'entities';
    dom.viewHint.textContent = isEntities
      ? 'Окрестность узла: выберите узел поиском или подсистему.'
      : 'Свод по файлам: видно, какие файлы зависят от каких.';
    dom.group.disabled = !isEntities;
    dom.depth.disabled = !isEntities;
    dom.direction.disabled = !isEntities;
    for (const box of dom.relations.querySelectorAll('input')) box.disabled = !isEntities;
    updateFiltersHint();
    if (isEntities && !state.focus && !state.group) {
      dom.counters.textContent = 'Выберите узел или подсистему';
    }
  }

  /** Подсказка под легендой: пустой набор галочек фильтром не считается — показываем всё. */
  function updateFiltersHint() {
    if (!dom.kindsHint || !dom.relationsHint) return;
    dom.kindsHint.textContent = state.checkedKinds.size
      ? 'Снятая галочка скрывает вид.'
      : 'Ничего не отмечено — показываю все виды узлов.';
    if (state.view !== 'entities') {
      dom.relationsHint.textContent = 'В своде по файлам связи сгруппированы: вызывает, импортирует, покрывает тестом, данные, карта, история.';
      return;
    }
    dom.relationsHint.textContent = state.checkedRelations.size
      ? 'Снятая галочка скрывает вид связи.'
      : 'Ничего не отмечено — показываю все виды связей.';
  }

  function reset() {
    state.focus = '';
    state.group = '';
    state.depth = 2;
    state.direction = 'both';
    state.limit = 600;
    state.checkedKinds = new Set(state.allKinds);
    state.checkedRelations = new Set(state.allRelations);
    dom.q.value = '';
    dom.group.value = '';
    dom.depth.value = '2';
    dom.direction.value = 'both';
    dom.viewModules.checked = true;
    renderKinds();
    renderRelations();
    setView('modules');
    loadData();
  }

  function bindEvents() {
    dom.form.addEventListener('submit', (event) => {
      event.preventDefault();
      const query = dom.q.value.trim();
      if (!query) {
        showNotes(['Введите имя функции, файл или роут.']);
        return;
      }
      SKKKK.guardButton(dom.search, async () => {
        state.view = 'entities';
        dom.viewEntities.checked = true;
        state.focus = query;
        state.group = '';
        dom.group.value = '';
        setView('entities');
        await loadData();
      }, { label: 'Ищу…' });
    });
    dom.viewModules.addEventListener('change', () => { setView('modules'); state.focus = ''; loadData(); });
    dom.viewEntities.addEventListener('change', () => { setView('entities'); loadData(); });
    dom.group.addEventListener('change', () => {
      state.group = dom.group.value;
      state.focus = '';
      dom.q.value = '';
      loadData();
    });
    dom.depth.addEventListener('change', () => { state.depth = Number(dom.depth.value) || 2; loadData(); });
    dom.direction.addEventListener('change', () => { state.direction = dom.direction.value; loadData(); });
    dom.reset.addEventListener('click', () => reset());
    dom.more.addEventListener('click', () => {
      state.limit = Math.min(LIMIT_MAX, state.limit * 2);
      loadData();
    });
    dom.nodeClose.addEventListener('click', () => clearNode());
  }

  function collectDom() {
    dom.form = document.getElementById('amg-search-form');
    dom.q = document.getElementById('amg-q');
    dom.search = document.getElementById('amg-search');
    dom.viewModules = document.getElementById('amg-view-modules');
    dom.viewEntities = document.getElementById('amg-view-entities');
    dom.group = document.getElementById('amg-group');
    dom.depth = document.getElementById('amg-depth');
    dom.direction = document.getElementById('amg-direction');
    dom.reset = document.getElementById('amg-reset');
    dom.more = document.getElementById('amg-limit-more');
    dom.hint = document.getElementById('amg-view-hint');
    dom.kinds = document.getElementById('amg-kinds');
    dom.relations = document.getElementById('amg-relations');
    dom.relationsHint = document.getElementById('amg-relations-hint');
    dom.kindsHint = document.getElementById('amg-kinds-hint');
    dom.counters = document.getElementById('amg-counters');
    dom.truncated = document.getElementById('amg-truncated');
    dom.stale = document.getElementById('amg-stale');
    dom.source = document.getElementById('amg-source');
    dom.notes = document.getElementById('amg-notes');
    dom.canvas = document.getElementById('amg-canvas');
    dom.node = document.getElementById('amg-node');
    dom.nodeTitle = document.getElementById('amg-node-title');
    dom.nodeKind = document.getElementById('amg-node-kind');
    dom.nodeClose = document.getElementById('amg-node-close');
    dom.nodeWhere = document.getElementById('amg-node-where');
    dom.nodeProps = document.getElementById('amg-node-props');
    dom.nodeIn = document.getElementById('amg-node-in');
    dom.nodeOut = document.getElementById('amg-node-out');
  }

  async function boot() {
    collectDom();
    if (!window.cytoscape) {
      showNotes(['Не удалось загрузить библиотеку графа. Обновите страницу.']);
      return;
    }
    bindEvents();
    setView('modules');
    const ready = await loadSummary();
    if (!ready) return;
    await loadData();
  }

  boot();
})();
