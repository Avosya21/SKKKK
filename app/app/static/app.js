const SKKKK = {
  setSession(data) {
    localStorage.setItem('skkkk_token', data.access_token);
    localStorage.setItem('skkkk_role', data.role);
    localStorage.setItem('skkkk_name', data.name);
    this.updateNav();
  },
  updateNav() {
    const el = document.getElementById('nav-user');
    const session = document.getElementById('nav-session');
    const name = localStorage.getItem('skkkk_name');
    const role = localStorage.getItem('skkkk_role');
    if (el) {
      el.textContent = name && role ? `${name} (${role})` : '';
    }
    if (session) {
      session.classList.toggle('d-none', !(name && role));
    }
  },
  async updateLlmBadge() {
    const el = document.getElementById('nav-llm');
    if (!el) return;
    try {
      const token = this.token();
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const r = await fetch('/health/ready', { credentials: 'same-origin', headers });
      if (!r.ok) return;
      const data = await r.json();
      const provider = data.llm_provider || '';
      const labels = { lmstudio: 'Gemma', cursor: 'Cursor' };
      el.textContent = labels[provider] || provider || 'LLM';
      el.classList.remove('d-none');
      el.title = data.llm_detail || provider;
      el.classList.toggle('text-bg-success', Boolean(data.llm));
      el.classList.toggle('text-bg-secondary', !data.llm);
    } catch {
      /* health недоступен — бейдж скрыт */
    }
  },
  token() {
    return localStorage.getItem('skkkk_token');
  },
  clearSession({ skipServer = false } = {}) {
    localStorage.removeItem('skkkk_token');
    localStorage.removeItem('skkkk_role');
    localStorage.removeItem('skkkk_name');
    this.updateNav();
    if (skipServer) return;
    try {
      fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' });
    } catch {
      /* cookie drop is best-effort */
    }
  },
  async logout() {
    try {
      await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' });
    } catch {
      /* cookie drop is best-effort */
    }
    this.clearSession({ skipServer: true });
    location.href = '/login';
  },
  requireAuth() {
    if (!this.token()) {
      location.href = '/login';
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
      if (r.status === 401) {
        this.clearSession();
        location.href = '/login';
      }
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
            detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
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
      const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
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
SKKKK.updateNav();
SKKKK.updateLlmBadge();
document.getElementById('nav-logout')?.addEventListener('click', () => {
  SKKKK.logout();
});
