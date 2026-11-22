// Общий JS портала (объект PrimaryAnalitics): сессия и навигация, бейдж LLM, API-запросы, блокировка кнопок, склонения.
const PrimaryAnalitics = {
  setSession(data) {
    localStorage.setItem('primary_analitics_token', data.access_token);
    localStorage.setItem('primary_analitics_role', data.role);
    localStorage.setItem('primary_analitics_name', data.name);
    if (localStorage.getItem('primary_analitics_session_name') !== data.name) {
      // Вошёл другой человек — имя и фото прежнего не показываем, пока не загрузится его карточка.
      localStorage.removeItem('primary_analitics_display');
      localStorage.removeItem('primary_analitics_avatar');
      localStorage.setItem('primary_analitics_session_name', data.name);
    }
    this.updateNav();
  },
  updateNav() {
    const el = document.getElementById('nav-user');
    const session = document.getElementById('nav-session');
    const avatar = document.getElementById('nav-avatar');
    // Имя из карточки («Имя Фамилия»), пока её нет — логин.
    const name = localStorage.getItem('primary_analitics_display') || localStorage.getItem('primary_analitics_name');
    const role = localStorage.getItem('primary_analitics_role');
    const photo = localStorage.getItem('primary_analitics_avatar');
    if (el) {
      el.textContent = name && role ? `${name} (${role})` : '';
    }
    if (avatar) {
      if (photo && role) {
        if (avatar.getAttribute('src') !== photo) avatar.src = photo;
        avatar.classList.remove('d-none');
      } else {
        avatar.classList.add('d-none');
      }
    }
    if (session) {
      session.classList.toggle('d-none', !(name && role));
    }
  },
  /** Склонение по числу: plural(1, ['срок','срока','сроков']) → 'срок'; plural(2, …) → 'срока'; plural(5, …) → 'сроков'. */
  plural(n, forms) {
    const abs = Math.abs(Number(n)) % 100;
    const last = abs % 10;
    if (abs > 10 && abs < 20) return forms[2];
    if (last > 1 && last < 5) return forms[1];
    if (last === 1) return forms[0];
    return forms[2];
  },
  count(n, forms) {
    return `${n} ${this.plural(n, forms)}`;
  },
  async updateLlmBadge() {
    const el = document.getElementById('nav-llm');
    if (!el) return;
    const staleEl = document.getElementById('nav-stale');
    // Без входа (страница логина, гость) опрос дал бы 401 в консоли — признак входа ставит login (primary_analitics_role).
    if (!localStorage.getItem('primary_analitics_role') && !this.token()) return;
    try {
      const token = this.token();
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const r = await fetch('/health/ready', { credentials: 'same-origin', headers });
      if (!r.ok) return;
      const data = await r.json();
      const provider = data.llm_provider || '';
      const labels = { lmstudio: 'Local LLM', cursor: 'Cursor' };
      el.textContent = labels[provider] || provider || 'LLM';
      el.classList.remove('d-none');
      el.title = data.llm_detail || provider;
      el.classList.toggle('text-bg-success', Boolean(data.llm));
      el.classList.toggle('text-bg-secondary', !data.llm);
      if (staleEl) {
        const build = data.build || {};
        const isAdmin = localStorage.getItem('primary_analitics_role') === 'admin';
        staleEl.classList.toggle('d-none', !(build.stale && isAdmin));
        const files = Array.from(new Set([].concat(build.api_changed || [], build.worker_changed || [])));
        const who = [build.api_stale ? 'API' : '', build.worker_stale ? 'воркер' : ''].filter(Boolean).join(' и ');
        staleEl.title = 'Код на диске новее запущенного: ' + (who || 'сервер') +
          (files.length ? '. Изменено: ' + files.join(', ') : '') + '. Выполните scripts/restart.ps1';
      }
    } catch {
      /* health недоступен — бейдж скрыт */
    }
  },
  token() {
    return localStorage.getItem('primary_analitics_token');
  },
  /** Адрес этой вкладки, на который можно вернуть после входа. Чужой сайт — пусто. */
  safeReturnPath(raw) {
    if (raw == null) return '';
    const text = String(raw).trim();
    if (!text || text.length > 2000 || !text.startsWith('/') || text.startsWith('//') || text.startsWith('/\\')) return '';
    if (/[\\\n\r\t ]/.test(text)) return '';
    const lowered = text.toLowerCase();
    if (text.includes('://') || lowered.includes('%5c') || lowered.includes('%2f%2f') || lowered.includes('%0a') || lowered.includes('%0d')) return '';
    const path = text.split('?')[0].split('#')[0];
    if (path === '/login' || path.startsWith('/login/')) return '';
    return text;
  },
  currentReturnPath() {
    if (location.pathname === '/login') return '';
    return this.safeReturnPath(location.pathname + location.search + location.hash);
  },
  rememberReturn() {
    const here = this.currentReturnPath();
    if (!here) return;
    try { sessionStorage.setItem('primary_analitics_return', here); } catch { /* приватный режим */ }
  },
  rememberReferrer() {
    if (this.tabReturnPath()) return;
    let ref;
    try { ref = new URL(document.referrer); } catch { return; }
    if (ref.origin !== location.origin) return;
    const safe = this.safeReturnPath(ref.pathname + ref.search + ref.hash);
    if (!safe) return;
    try { sessionStorage.setItem('primary_analitics_return', safe); } catch { /* приватный режим */ }
  },
  loginUrl() {
    const here = this.currentReturnPath();
    if (!here) return '/login';
    return '/login?next=' + encodeURIComponent(here);
  },
  goLogin() {
    this.rememberReturn();
    location.href = this.loginUrl();
  },
  tabReturnPath() {
    let stored = '';
    try { stored = sessionStorage.getItem('primary_analitics_return') || ''; } catch { stored = ''; }
    const fromQuery = new URLSearchParams(location.search).get('next') || '';
    return this.safeReturnPath(fromQuery) || this.safeReturnPath(stored) || '';
  },
  /** Страница входа при уже живой cookie: вернуть страницу этой вкладки. */
  async resumeLoginTab() {
    if (location.pathname !== '/login') return false;
    if (this._resumingLogin) return false;
    this._resumingLogin = true;
    try {
      this.rememberReferrer();
      let response;
      try {
        response = await fetch('/me/profile', { credentials: 'same-origin' });
      } catch {
        return false;
      }
      if (!response.ok) return false;
      location.replace(this.tabReturnPath() || '/');
      return true;
    } finally {
      this._resumingLogin = false;
    }
  },
  clearSession({ skipServer = false } = {}) {
    localStorage.removeItem('primary_analitics_token');
    localStorage.removeItem('primary_analitics_role');
    localStorage.removeItem('primary_analitics_name');
    localStorage.removeItem('primary_analitics_display');
    localStorage.removeItem('primary_analitics_avatar');
    this.updateNav();
    if (skipServer) return;
    try {
      fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' });
    } catch {
      /* cookie drop is best-effort */
    }
  },
  async logout() {
    this.rememberReturn();
    const next = this.loginUrl();
    try {
      await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' });
    } catch {
      /* cookie drop is best-effort */
    }
    this.clearSession({ skipServer: true });
    location.href = next;
  },
  requireAuth() {
    if (!this.token()) {
      this.goLogin();
      return false;
    }
    this.updateNav();
    return true;
  },

  /** Зафиксировать ширину и показать спиннер (как у «Скачать»). */
  setButtonBusy(btn, busy, { label } = {}) {
    if (!btn) return;
    const idle = btn.dataset.idleLabel || (btn.textContent || '').trim();
    if (!btn.dataset.idleLabel) btn.dataset.idleLabel = idle;
    if (busy) {
      if (!btn.dataset.minWidth && btn.offsetWidth) {
        btn.dataset.minWidth = String(Math.ceil(btn.offsetWidth));
        btn.style.minWidth = `${btn.dataset.minWidth}px`;
      }
      btn.disabled = true;
      btn.dataset.busy = '1';
      btn.setAttribute('aria-busy', 'true');
      btn.innerHTML =
        '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>';
      if (label) btn.setAttribute('aria-label', label);
      else btn.setAttribute('aria-label', idle);
    } else {
      btn.disabled = false;
      btn.dataset.busy = '0';
      btn.removeAttribute('aria-busy');
      btn.textContent = idle;
      btn.setAttribute('aria-label', idle);
    }
  },

  /** Блокировка повторного нажатия на action-кнопку. Возвращает false, если уже занята. */
  async guardButton(btn, work, { label } = {}) {
    if (!btn || btn.dataset.busy === '1' || btn.disabled) return false;
    this.setButtonBusy(btn, true, { label });
    try {
      await work();
      return true;
    } finally {
      this.setButtonBusy(btn, false);
    }
  },

  /**
   * Prevent double-taps on slow mobile networks: lock the form until async work finishes.
   * Returns false if already locked (caller should abort).
   */
  async guardSubmit(form, work, { busyText = 'Подождите…' } = {}) {
    if (!form || form.dataset.submitting === '1') return false;
    form.dataset.submitting = '1';
    form.setAttribute('aria-busy', 'true');

    const controls = Array.from(
      form.querySelectorAll('button, input[type="submit"], input:not([type="hidden"]), select, textarea'),
    );
    const snapshot = controls.map((el) => ({
      el,
      disabled: el.disabled,
    }));
    const submitBtn =
      form.querySelector('button[type="submit"], input[type="submit"]') ||
      form.querySelector('button.btn-primary');

    for (const el of controls) {
      if (el.matches('button, input[type="submit"]')) el.disabled = true;
    }
    if (submitBtn && submitBtn.tagName === 'BUTTON') {
      this.setButtonBusy(submitBtn, true, { label: busyText });
    }

    try {
      await work();
      return true;
    } finally {
      form.dataset.submitting = '0';
      form.removeAttribute('aria-busy');
      for (const s of snapshot) {
        if (s.el === submitBtn) continue;
        s.el.disabled = s.disabled;
      }
      if (submitBtn && submitBtn.tagName === 'BUTTON') {
        this.setButtonBusy(submitBtn, false);
      }
    }
  },
  api(url, opts = {}) {
    const headers = new Headers(opts.headers || {});
    const token = this.token();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    return fetch(url, { ...opts, headers }).then((r) => {
      if (r.status !== 401 || location.pathname === '/login') return r;
      const sent = headers.get('Authorization') || '';
      const sentToken = sent.toLowerCase().startsWith('bearer ') ? sent.slice(7).trim() : '';
      const current = this.token() || '';
      // Устаревший запрос: пока он шёл, сессия уже другая. Новую не гасим.
      if (current && sentToken !== current) return r;
      this.goLogin();
      this.clearSession({ skipServer: !sentToken });
      return r;
    });
  },

  async downloadArtifact(artifactId, { onStart, onProgress, onComplete } = {}) {
    if (!this._downloadLocks) this._downloadLocks = new Set();
    if (this._downloadLocks.has(artifactId)) {
      return { status: 'busy' };
    }
    this._downloadLocks.add(artifactId);
    try {
      if (typeof onStart === 'function') onStart();
      const r = await this.api(`/artifacts/${artifactId}/download`);
      if (!r.ok) {
        let detail = `HTTP ${r.status}`;
        try {
          const body = await r.json();
          if (body && body.detail) {
            detail = window.PrimaryAnaliticsErrors ? PrimaryAnaliticsErrors.text(body.detail, r.status) : String(body.detail);
          }
        } catch {
          /* not json */
        }
        throw new Error(detail);
      }
      const total = Number(r.headers.get('Content-Length')) || 0;
      let blob;
      if (r.body && typeof onProgress === 'function') {
        const reader = r.body.getReader();
        const parts = [];
        let received = 0;
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          parts.push(value);
          received += value.length;
          const percent = total ? Math.min(100, Math.round((received / total) * 100)) : null;
          onProgress({ received, total, percent });
        }
        blob = new Blob(parts);
      } else {
        blob = await r.blob();
      }
      if (!blob.size) {
        throw new Error('Пустой файл');
      }
      let name = 'download';
      const cd = r.headers.get('Content-Disposition') || '';
      const star = cd.match(/filename\*=UTF-8''([^;]+)/i);
      const plain = cd.match(/filename="([^"]+)"/i);
      if (star) name = decodeURIComponent(star[1]);
      else if (plain) name = plain[1];
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      a.rel = 'noopener';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      const result = { status: 'ok', filename: name, bytes: blob.size };
      if (typeof onComplete === 'function') onComplete(result);
      return result;
    } finally {
      this._downloadLocks.delete(artifactId);
    }
  },

  async apiJson(url, opts = {}) {
    const r = await this.api(url, opts);
    let body = null;
    const text = await r.text();
    if (text) {
      try {
        body = JSON.parse(text);
      } catch {
        body = text;
      }
    }
    if (!r.ok) {
      const detail = body && body.detail !== undefined ? body.detail : body;
      const raw = typeof detail === 'string' ? detail : JSON.stringify(detail);
      const err = new Error(window.PrimaryAnaliticsErrors ? PrimaryAnaliticsErrors.text(detail, r.status) : raw);
      err.detail = raw;
      err.status = r.status;
      err.body = body;
      throw err;
    }
    return body;
  },

  /**
   * Chunked resumable upload with retries and progress callback.
   * onProgress({ file, fileIndex, filesTotal, sentBytes, totalBytes, percent, phase, error })
   */
  async uploadFilesResumable(projectId, files, sourceType, { onProgress, concurrency = 2, maxRetries = 5 } = {}) {
    const list = Array.from(files || []);
    const results = [];
    let globalSent = 0;
    const globalTotal = list.reduce((s, f) => s + (f.size || 0), 0);

    const report = (partial) => {
      if (typeof onProgress === 'function') {
        onProgress({
          filesTotal: list.length,
          sentBytes: globalSent,
          totalBytes: globalTotal,
          percent: globalTotal ? Math.min(100, Math.round((globalSent / globalTotal) * 100)) : 100,
          ...partial,
        });
      }
    };

    for (let fileIndex = 0; fileIndex < list.length; fileIndex += 1) {
      const file = list[fileIndex];
      const clientKey = `${file.name}:${file.size}:${file.lastModified || 0}`;
      report({ file, fileIndex, phase: 'init' });

      const session = await this.apiJson(`/projects/${projectId}/uploads/init`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename: file.name,
          size: file.size,
          source_type: sourceType,
          client_key: clientKey,
        }),
      });

      const chunkSize = session.chunk_size;
      const totalChunks = session.total_chunks;
      const have = new Set(session.received || []);
      const counted = new Set(have);
      // Count already-received bytes toward progress (resume).
      let fileSent = 0;
      for (const idx of have) {
        const start = idx * chunkSize;
        const end = Math.min(start + chunkSize, file.size);
        fileSent += end - start;
      }
      globalSent += fileSent;
      report({ file, fileIndex, phase: 'upload', percent: globalTotal ? Math.round((globalSent / globalTotal) * 100) : 100 });

      const pending = [];
      for (let i = 0; i < totalChunks; i += 1) {
        if (!have.has(i)) pending.push(i);
      }

      const uploadOne = async (index) => {
        const start = index * chunkSize;
        const end = Math.min(start + chunkSize, file.size);
        const blob = file.slice(start, end);
        let lastErr = null;
        for (let attempt = 1; attempt <= maxRetries; attempt += 1) {
          try {
            report({ file, fileIndex, phase: 'upload', attempt });
            const r = await this.api(
              `/projects/${projectId}/uploads/${session.upload_id}/chunks/${index}`,
              {
                method: 'PUT',
                headers: { 'Content-Type': 'application/octet-stream' },
                body: blob,
              },
            );
            if (r.status === 401) return;
            if (!r.ok) {
              const text = await r.text();
              throw new Error(text || `chunk ${index} HTTP ${r.status}`);
            }
            if (!counted.has(index)) {
              counted.add(index);
              globalSent += end - start;
            }
            report({ file, fileIndex, phase: 'upload' });
            return;
          } catch (err) {
            lastErr = err;
            report({ file, fileIndex, phase: 'retry', error: String(err.message || err), attempt });
            await new Promise((resolve) => setTimeout(resolve, Math.min(8000, 400 * 2 ** (attempt - 1))));
          }
        }
        throw lastErr || new Error(`chunk ${index} failed`);
      };

      let cursor = 0;
      const workers = Array.from({ length: Math.min(concurrency, pending.length || 1) }, async () => {
        while (cursor < pending.length) {
          const idx = pending[cursor];
          cursor += 1;
          await uploadOne(idx);
        }
      });
      if (pending.length) await Promise.all(workers);

      report({ file, fileIndex, phase: 'complete' });
      const art = await this.apiJson(`/projects/${projectId}/uploads/${session.upload_id}/complete`, {
        method: 'POST',
      });
      results.push(art);
      report({ file, fileIndex, phase: 'done' });
    }

    return results;
  },

  /**
   * Gemma often emits typographic LaTeX ($\\rightarrow$, $\\times$, …).
   * Turn those scraps into Unicode; we do not load a math renderer.
   */
  plainifyLlmMarkup(text) {
    if (!text) return '';
    let out = String(text);
    const cmds = [
      ['longrightarrow', '→'], ['longleftarrow', '←'], ['leftrightarrow', '↔'],
      ['Leftrightarrow', '⇔'], ['Rightarrow', '⇒'], ['Leftarrow', '⇐'],
      ['rightarrow', '→'], ['leftarrow', '←'], ['uparrow', '↑'], ['downarrow', '↓'],
      ['hookrightarrow', '↪'], ['mapsto', '↦'], ['leqslant', '≤'], ['geqslant', '≥'],
      ['subseteq', '⊆'], ['supseteq', '⊇'], ['emptyset', '∅'], ['varnothing', '∅'],
      ['checkmark', '✓'], ['partial', '∂'], ['forall', '∀'], ['exists', '∃'],
      ['subset', '⊂'], ['supset', '⊃'], ['approx', '≈'], ['simeq', '≃'],
      ['equiv', '≡'], ['propto', '∝'], ['infty', '∞'], ['times', '×'],
      ['cdot', '·'], ['bullet', '•'], ['ldots', '…'], ['cdots', '⋯'], ['dots', '…'],
      ['degree', '°'], ['nabla', '∇'], ['angle', '∠'], ['perp', '⊥'],
      ['parallel', '∥'], ['notin', '∉'], ['leq', '≤'], ['geq', '≥'], ['neq', '≠'],
      ['sum', '∑'], ['prod', '∏'], ['sqrt', '√'], ['cap', '∩'], ['cup', '∪'],
      ['sim', '∼'], ['pm', '±'], ['mp', '∓'], ['ll', '≪'], ['gg', '≫'],
      ['le', '≤'], ['ge', '≥'], ['ne', '≠'], ['to', '→'], ['in', '∈'],
    ];
    cmds.sort((a, b) => b[0].length - a[0].length);
    for (const [name, glyph] of cmds) {
      const re = new RegExp(
        String.raw`(?:\$\s*)?\\${name}(?![A-Za-z])(?:\s*\$)?|\\\(\s*\\${name}(?![A-Za-z])\s*\\\)`,
        'gi',
      );
      out = out.replace(re, glyph);
    }
    const unwrap = (inner) => {
      let s = String(inner || '').trim();
      for (const [name, glyph] of cmds) {
        s = s.replace(new RegExp(String.raw`\\${name}(?![A-Za-z])`, 'gi'), glyph);
      }
      return s;
    };
    out = out.replace(/\$([^$\n]{1,120})\$/g, (_, inner) => unwrap(inner));
    out = out.replace(/\\\(([\s\S]*?)\\\)/g, (_, inner) => unwrap(inner));
    out = out.replace(/\\\[([\s\S]*?)\\\]/g, (_, inner) => unwrap(inner));
    out = out
      .replace(/\\%/g, '%')
      .replace(/\\&/g, '&')
      .replace(/\\#/g, '#')
      .replace(/\\_/g, '_')
      .replace(/\\\{/g, '{')
      .replace(/\\\}/g, '}');
    out = out.replace(/\$([^\w\s$]{1,4})\$/g, '$1');
    return out;
  },

  /** Emoji и markdown-чекбоксы [ ] в итоговых выжимках. */
  sanitizeSummaryMarkdown(text) {
    if (!text) return '';
    let out = String(text);
    out = out.replace(
      /[\u{1F1E0}-\u{1F1FF}\u{1F300}-\u{1F5FF}\u{1F600}-\u{1F64F}\u{1F680}-\u{1F6FF}\u{1F700}-\u{1F77F}\u{1F780}-\u{1F7FF}\u{1F800}-\u{1F8FF}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FAFF}\u{2600}-\u{26FF}\u{2702}-\u{27B0}]/gu,
      '',
    );
    out = out.replace(/^(\s*(?:[-*]|\d+\.)\s+)\[[ xX]\]\s+/gm, '$1');
    out = out.replace(/^\s*\[[ xX]\]\s+/gm, '');
    out = out.replace(/[ \t]{2,}/g, ' ');
    out = out.replace(/ +\n/g, '\n');
    return out.trim();
  },

  cleanSummaryOutput(text) {
    return this.sanitizeSummaryMarkdown(this.plainifyLlmMarkup(text));
  },
};
PrimaryAnalitics.updateNav();
PrimaryAnalitics.updateLlmBadge();
setInterval(() => PrimaryAnalitics.updateLlmBadge(), 60000);
// Вход в другой вкладке пишет токен в localStorage. Эта вкладка, если на ней форма входа, сразу возвращает свою страницу.
window.addEventListener('storage', (event) => {
  if (event.key === 'primary_analitics_token' && event.newValue && location.pathname === '/login') {
    PrimaryAnalitics.resumeLoginTab();
  }
});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && location.pathname === '/login' && PrimaryAnalitics.token()) {
    PrimaryAnalitics.resumeLoginTab();
  }
});
document.getElementById('nav-logout')?.addEventListener('click', () => {
  PrimaryAnalitics.logout();
});

// Меню пользователя в шапке: «Моя карточка», «Личный пароль» (если личные пароли включены), «Настройки».
(function () {
  const $ = (id) => document.getElementById(id);
  const passBtn = $('nav-password');
  const profileBtn = $('nav-profile');
  const uiBtn = $('nav-ui');
  const passForm = $('password-form');
  const meForm = $('me-form');
  if (!passBtn || !passForm || !uiBtn || !localStorage.getItem('primary_analitics_role') || location.pathname === '/login') return;
  let state = null;
  let me = null;
  let newPhoto = null;
  let uiHome = 'list';

  function syncMenu() {
    const any = !passBtn.classList.contains('d-none') || !profileBtn.classList.contains('d-none') || !uiBtn.classList.contains('d-none');
    // Пустое меню не раскрываем.
    if (any) $('nav-me').setAttribute('data-bs-toggle', 'dropdown');
    else $('nav-me').removeAttribute('data-bs-toggle');
  }
  syncMenu();

  function applyProfile(data) {
    me = data;
    if (data.full_name) localStorage.setItem('primary_analitics_display', data.full_name);
    if (data.avatar_url) localStorage.setItem('primary_analitics_avatar', data.avatar_url);
    profileBtn.classList.toggle('d-none', !data.complete);
    PrimaryAnalitics.updateNav();
    syncMenu();
  }

  PrimaryAnalitics.api('/auth/password/state').then(async (r) => {
    if (!r.ok) return;
    state = await r.json();
    passBtn.classList.toggle('d-none', state.mode === 'off');
    syncMenu();
  }).catch(() => {});

  PrimaryAnalitics.api('/me/profile').then(async (r) => {
    if (r.ok) applyProfile(await r.json());
  }).catch(() => {});

  function showError(id, text) {
    $(id).textContent = text;
    $(id).classList.toggle('d-none', !text);
  }

  function readJson(r) {
    return r.json().catch(() => null);
  }

  function apiError(payload, status) {
    return window.PrimaryAnaliticsErrors ? PrimaryAnaliticsErrors.text(payload && payload.detail, status) : String(status);
  }

  // --- личный пароль ---
  passBtn.addEventListener('click', () => {
    if (!state) return;
    for (const id of ['password-current', 'password-new', 'password-confirm']) $(id).value = '';
    $('password-username').value = localStorage.getItem('primary_analitics_name') || '';
    $('password-current-group').classList.toggle('d-none', !state.has_password);
    $('password-title').textContent = state.has_password ? 'Сменить личный пароль' : 'Создать личный пароль';
    $('password-new-help').textContent = `Не короче ${state.min_length} символов. Не совпадает с логином и паролем роли.`;
    showError('password-error', '');
    bootstrap.Modal.getOrCreateInstance($('passwordModal')).show();
  });
  $('passwordModal').addEventListener('shown.bs.modal', () => {
    $(state && state.has_password ? 'password-current' : 'password-new').focus();
  });

  passForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      current_password: $('password-current').value,
      password: $('password-new').value,
      password_confirm: $('password-confirm').value,
    };
    if (body.password.length < state.min_length) return showError('password-error', `Пароль короче ${state.min_length} символов`);
    if (body.password !== body.password_confirm) return showError('password-error', 'Пароли не совпадают');
    showError('password-error', '');
    await PrimaryAnalitics.guardSubmit(passForm, async () => {
      const r = await fetch('/auth/password/change', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${PrimaryAnalitics.token()}` },
        body: JSON.stringify(body),
      });
      const payload = await readJson(r);
      if (!r.ok) {
        showError('password-error', apiError(payload, r.status));
        return;
      }
      PrimaryAnalitics.setSession(payload);
      state.has_password = true;
      bootstrap.Modal.getOrCreateInstance($('passwordModal')).hide();
    }, { busyText: 'Сохраняю…' });
  });

  // --- моя карточка ---
  function setPhotoPreview(url) {
    $('me-photo').src = url || '';
  }

  function pickPhoto(file) {
    if (!file) return;
    if (!file.type.startsWith('image/')) return showError('me-error', 'Выберите фото (JPEG или PNG)');
    if (file.size > 10 * 1024 * 1024) return showError('me-error', 'Фото больше 10 МБ');
    newPhoto = file;
    setPhotoPreview(URL.createObjectURL(file));
    showError('me-error', '');
  }
  $('me-photo-input').addEventListener('change', (e) => pickPhoto(e.target.files[0]));
  $('me-camera-input').addEventListener('change', (e) => pickPhoto(e.target.files[0]));

  profileBtn.addEventListener('click', () => {
    if (!me) return;
    newPhoto = null;
    $('me-last').value = me.last_name;
    $('me-first').value = me.first_name;
    $('me-job').value = me.job_title;
    $('me-company').textContent = me.company || '—';
    setPhotoPreview(me.avatar_url ? me.avatar_url.replace('size=96', 'size=512') : '');
    $('me-consent').textContent = me.voice_consent_at
      ? `Голос используется для распознавания в записях с ${new Date(me.voice_consent_at).toLocaleDateString()}. Отозвать — через администратора.`
      : 'Согласие на распознавание голоса отозвано.';
    $('me-camera-btn').classList.toggle('d-none', !matchMedia('(pointer: coarse)').matches);
    showError('me-error', '');
    bootstrap.Modal.getOrCreateInstance($('profileModal')).show();
  });

  meForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      last_name: $('me-last').value.trim(),
      first_name: $('me-first').value.trim(),
      job_title: $('me-job').value.trim(),
    };
    if (!body.last_name || !body.first_name || body.job_title.length < 2) {
      return showError('me-error', 'Заполните фамилию, имя и должность');
    }
    showError('me-error', '');
    await PrimaryAnalitics.guardSubmit(meForm, async () => {
      if (newPhoto) {
        const fd = new FormData();
        fd.append('avatar', newPhoto, newPhoto.name || 'photo.jpg');
        const r = await PrimaryAnalitics.api('/me/avatar', { method: 'PUT', body: fd });
        const payload = await readJson(r);
        if (!r.ok) return showError('me-error', apiError(payload, r.status));
        newPhoto = null;
        applyProfile(payload);
      }
      const r = await PrimaryAnalitics.api('/me/profile', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const payload = await readJson(r);
      if (!r.ok) return showError('me-error', apiError(payload, r.status));
      applyProfile(payload);
      bootstrap.Modal.getOrCreateInstance($('profileModal')).hide();
    }, { busyText: 'Сохраняю…' });
  });

  function markHome(home) {
    uiHome = home === 'dashboard' ? 'dashboard' : 'list';
    $('ui-home-list').checked = uiHome === 'list';
    $('ui-home-dashboard').checked = uiHome === 'dashboard';
  }

  uiBtn.addEventListener('click', async () => {
    showError('ui-error', '');
    const r = await PrimaryAnalitics.api('/me/ui');
    if (r.ok) markHome((await r.json()).home);
    else markHome(uiHome);
    bootstrap.Modal.getOrCreateInstance($('uiModal')).show();
  });

  async function saveHome(home) {
    if (home === uiHome) return;
    const previous = uiHome;
    markHome(home);
    const r = await PrimaryAnalitics.api('/me/ui', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ home }),
    });
    if (!r.ok) {
      markHome(previous);
      showError('ui-error', 'Не удалось сохранить настройку.');
      return;
    }
    showError('ui-error', '');
    window.dispatchEvent(new CustomEvent('primary-analitics-home', { detail: { home } }));
  }
  $('ui-home-list').addEventListener('change', () => { if ($('ui-home-list').checked) saveHome('list'); });
  $('ui-home-dashboard').addEventListener('change', () => { if ($('ui-home-dashboard').checked) saveHome('dashboard'); });

  // Jira: личный токен в том же окне. Блок скрыт, пока на сервере не задан адрес Jira.
  const jiraBox = $('jira-settings');
  const jiraForm = $('jira-connect-form');
  if (jiraBox && jiraForm) {
    async function loadJiraStatus(extra) {
      const r = await PrimaryAnalitics.api('/jira/status');
      if (!r.ok) return null;
      const data = await r.json();
      jiraBox.classList.toggle('d-none', !data.available);
      if (!data.available) return data;
      $('jira-disconnected').classList.toggle('d-none', !!data.connected);
      $('jira-connected').classList.toggle('d-none', !data.connected);
      $('jira-connected-name').textContent = data.connected ? 'подключена: ' + (data.name || 'токен принят') : '';
      $('jira-pick-btn').classList.toggle('d-none', !(data.connected && data.can_import && location.pathname === '/'));
      showError('jira-error', data.connected && data.error ? 'Jira: ' + data.error : '');
      const detail = Object.assign({}, data, extra || {});
      window.PrimaryAnaliticsJira = { state: detail };
      window.dispatchEvent(new CustomEvent('primary-analitics-jira', { detail }));
      return data;
    }

    jiraForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const input = $('jira-token');
      const btn = $('jira-connect-btn');
      showError('jira-error', '');
      PrimaryAnalitics.setButtonBusy(btn, true, { label: 'Проверяю…' });
      try {
        const r = await PrimaryAnalitics.api('/jira/connect', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: input.value.trim() }),
        });
        if (!r.ok) {
          const payload = await readJson(r);
          showError('jira-error', apiError(payload, r.status));
          return;
        }
        input.value = '';
        await loadJiraStatus({ reloadProjects: true, justConnected: true });
      } finally {
        PrimaryAnalitics.setButtonBusy(btn, false);
      }
    });

    $('jira-disconnect-btn').addEventListener('click', async () => {
      if (!window.confirm('Отключить Jira? Токен будет удалён. Проекты, доступ к которым выдан по Jira, пропадут из вашего списка; загруженные материалы останутся.')) return;
      const r = await PrimaryAnalitics.api('/jira/connect', { method: 'DELETE' });
      if (!r.ok) {
        const payload = await readJson(r);
        showError('jira-error', apiError(payload, r.status));
        return;
      }
      await loadJiraStatus({ reloadProjects: true });
    });

    loadJiraStatus();
  }
})();


// Отправка JS-ошибок на сервер (пункт 5.2): не больше 5 за загрузку страницы, без персональных данных.
(function () {
  let sent = 0;
  function report(message, source) {
    if (sent >= 5) return;
    sent += 1;
    try {
      const body = JSON.stringify({ page: location.pathname.replace(/\/m\/[^/]+/, '/m/…'), message: String(message || ''), source: String(source || '') });
      if (navigator.sendBeacon) navigator.sendBeacon('/client-log', new Blob([body], { type: 'application/json' }));
      else fetch('/client-log', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body, keepalive: true });
    } catch (_) { /* логирование не должно ломать страницу */ }
  }
  window.addEventListener('error', (ev) => report(ev.message, (ev.filename || '') + ':' + (ev.lineno || '')));
  window.addEventListener('unhandledrejection', (ev) => report('unhandledrejection: ' + ((ev.reason && ev.reason.message) || ev.reason), ''));
})();
