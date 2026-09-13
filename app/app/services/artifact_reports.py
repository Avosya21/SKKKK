from __future__ import annotations

import json
import sys
from html import escape
from pathlib import Path

from app.app.config import ROOT, Settings
from app.app.models.artifact import Artifact
from app.app.services.entities import EntitiesPayload
from app.app.services.plain_text import clean_summary_output, has_llm_markup_artifacts, plainify_llm_markup
from app.app.services.storage import parsed_dir, structured_dir

_ASR = ROOT / "asr"
if str(_ASR) not in sys.path:
    sys.path.insert(0, str(_ASR))

from transcript_view import _summary_to_html  # noqa: E402

DOC_REPORT_MARKER = 'data-format="doc-summary-v2"'


def document_report_path(settings: Settings, artifact: Artifact) -> Path:
    out_dir = parsed_dir(settings, artifact.project_id, artifact.source_type)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{artifact.artifact_id}_report.html"


def document_report_url(artifact: Artifact) -> str:
    return f"/artifacts/{artifact.artifact_id}/report"


def write_document_report(
    settings: Settings,
    artifact: Artifact,
    payload: EntitiesPayload,
    text: str,
    *,
    summary_md: str = "",
) -> Path:
    path = document_report_path(settings, artifact)
    if not summary_md:
        from app.app.services.summarize_document import load_document_summary_md

        summary_md = load_document_summary_md(settings, artifact)
    html = _render_document_report(artifact, payload, text, summary_md=summary_md)
    path.write_text(html, encoding="utf-8")
    return path


def ensure_document_report(settings: Settings, artifact: Artifact) -> Path:
    path = document_report_path(settings, artifact)
    if path.is_file():
        body = path.read_text(encoding="utf-8")
        if DOC_REPORT_MARKER in body and not has_llm_markup_artifacts(body):
            return path
    payload = load_document_payload(settings, artifact)
    text = artifact.transcription_text or ""
    return write_document_report(settings, artifact, payload, text)


def load_document_payload(settings: Settings, artifact: Artifact) -> EntitiesPayload:
    path = structured_dir(settings, artifact.project_id) / f"{artifact.artifact_id}_entities.json"
    if path.is_file():
        try:
            return EntitiesPayload.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    summary = (artifact.transcription_text or "").strip()[:500]
    return EntitiesPayload(
        artifact_id=artifact.artifact_id,
        source_type=artifact.source_type,
        summary=summary,
    )


def _render_document_report(
    artifact: Artifact,
    payload: EntitiesPayload,
    text: str,
    *,
    summary_md: str = "",
) -> str:
    del payload
    title = escape(artifact.original_filename)
    art_id = escape(artifact.artifact_id)
    protocol = clean_summary_output((summary_md or "").strip())
    source = plainify_llm_markup(text or "")
    if protocol:
        summary_body = _summary_to_html(protocol)
    else:
        summary_body = '<p class="summary-empty">Итоговое резюме ещё не собрано. Нажмите «Пересобрать выжимку».</p>'
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{ font-family: Segoe UI, system-ui, sans-serif; color: #0f172a; background: #f8fafc; --accent: #5b7fa6; --border: #e2e8f0; }}
    body {{ margin: 0; font-size: .95rem; }}
    header, main {{ max-width: 980px; margin: 0 auto; padding: 16px; }}
    h1 {{ font-size: 1.35rem; margin: 0 0 8px; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 0; align-items: stretch; }}
    button.act, a.act {{
      border: 1px solid #cbd5e1; background: #fff; border-radius: 10px;
      padding: 8px 14px; cursor: pointer; font: inherit; font-size: inherit;
      box-sizing: border-box; max-width: 100%; color: inherit; text-decoration: none;
      display: inline-flex; align-items: center; justify-content: center;
      text-align: center; line-height: 1.3; min-height: 2.6rem;
      flex: 1 1 13rem; min-width: min(100%, 11rem);
    }}
    button.act:hover, a.act:hover {{ background: #e2e8f0; }}
    button.act.primary {{ background: var(--accent); color: #fff; border-color: transparent; font-weight: 600; }}
    button.act.primary:hover {{ background: #4e6e91; color: #fff; }}
    button.act:disabled {{ opacity: 0.65; cursor: wait; }}
    button.act:focus-visible, a.act:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
    .summary {{ background: #fff; border: 1px solid var(--border); border-radius: 12px; margin: 12px 0 20px; overflow: hidden; }}
    .summary-tabs {{ display: flex; flex-wrap: wrap; gap: 0; border-bottom: 1px solid var(--border); padding: 0 8px; }}
    .summary-tab {{
      border: 0; background: transparent; font: inherit; font-size: 1rem; font-weight: 600;
      color: #0f172a; padding: 12px 14px 10px; margin-bottom: -1px;
      border-bottom: 2px solid var(--accent);
    }}
    .summary-panel {{ padding: 14px 18px; }}
    .summary-empty {{ color: #64748b; margin: 4px 0; }}
    .summary h3 {{ margin: 14px 0 6px; font-size: 1rem; color: #1e293b; }}
    .summary h3:first-of-type {{ margin-top: 4px; }}
    .summary h4 {{ margin: 8px 0 4px; font-size: .92rem; }}
    .summary ul, .summary ol {{ margin: 4px 0 10px 1.25rem; padding: 0; }}
    .summary li {{ margin: 3px 0; line-height: 1.5; }}
    .summary p {{ margin: 4px 0 8px; line-height: 1.55; }}
    .summary-table-wrap {{ overflow-x: auto; margin: 8px 0 12px; -webkit-overflow-scrolling: touch; }}
    .summary-table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
    .summary-table th, .summary-table td {{
      border: 1px solid var(--border); padding: 6px 10px; text-align: left; vertical-align: top;
    }}
    .summary-table th {{ background: #f1f5f9; font-weight: 650; white-space: nowrap; }}
    .summary-table tbody tr:nth-child(even) {{ background: #f8fafc; }}
    .export-panel {{
      background: #fff; border: 1px solid var(--border); border-radius: 12px;
      padding: 12px 14px; margin: 0 0 16px;
    }}
    .export-panel-title {{ font-weight: 650; font-size: .95rem; margin-bottom: 8px; color: #1e293b; }}
    .source-collapse {{
      display: block; background: #fff; border: 1px solid var(--border);
      border-radius: 12px; margin-bottom: 16px;
    }}
    .source-summary {{
      cursor: pointer; list-style: none; display: flex; align-items: center; gap: 8px;
      padding: 10px 12px; font-size: inherit; border-radius: 12px;
    }}
    .source-summary:hover {{ background: #f8fafc; }}
    .source-summary:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
    .source-summary::-webkit-details-marker {{ display: none; }}
    .reg-toggle {{
      flex: 0 0 auto; width: .85rem; color: #64748b; font-size: .7rem;
      transition: transform .15s ease;
    }}
    .source-collapse[open] .reg-toggle {{ transform: rotate(90deg); }}
    .source-hint {{ margin-left: auto; color: #94a3b8; font-size: .78rem; font-weight: 400; }}
    .source-collapse[open] .source-hint-open {{ display: none; }}
    .source-hint-close {{ display: none; }}
    .source-collapse[open] .source-hint-close {{ display: inline; }}
    .source-body {{ padding: 0 12px 12px; }}
    .source-body pre {{
      white-space: pre-wrap; word-break: break-word; margin: 0;
      font-family: Segoe UI, system-ui, sans-serif; font-size: .92rem;
      line-height: 1.5; color: #334155;
    }}
    .action-spinner {{
      display: inline-block; width: 0.875rem; height: 0.875rem;
      border: 2px solid currentColor; border-right-color: transparent;
      border-radius: 50%; animation: export-spin 0.65s linear infinite; margin-right: 6px;
    }}
    @keyframes export-spin {{ to {{ transform: rotate(360deg); }} }}
    @media (max-width: 640px) {{
      header, main {{ padding: 12px; }}
      h1 {{ font-size: 1.15rem; }}
      button.act, a.act {{ flex: 1 1 100%; min-width: 0; }}
      .summary-table {{ font-size: .85rem; }}
      .summary-table th {{ white-space: normal; }}
    }}
  </style>
</head>
<body {DOC_REPORT_MARKER} data-artifact-id="{art_id}">
  <header>
    <h1>{title}</h1>
  </header>
  <main>
    <section class="summary" aria-label="Резюме документа">
      <div class="summary-tabs">
        <span class="summary-tab" id="doc-summary-heading">Итоговое резюме</span>
      </div>
      <div class="summary-panel" id="doc-summary" aria-labelledby="doc-summary-heading">
        {summary_body}
      </div>
    </section>
    {_toolbar_html(artifact.artifact_id)}
    {_source_section(source)}
  </main>
  {_document_report_script()}
</body>
</html>
"""


def _toolbar_html(artifact_id: str) -> str:
    aid = escape(artifact_id)
    return (
        '<section class="export-panel">'
        '<div class="export-panel-title">Скачать материалы</div>'
        '<div class="toolbar" role="toolbar" aria-label="Действия с документом">'
        '<button type="button" class="act primary" id="rebuild-summary" data-idle-label="Пересобрать выжимку">'
        "Пересобрать выжимку</button>"
        f'<a class="act" href="/artifacts/{aid}/download/summary.docx">Выжимка Word</a>'
        f'<a class="act" href="/artifacts/{aid}/download/transcript.txt">Полный текст</a>'
        "</div></section>"
    )


def _document_report_script() -> str:
    return """<script>
function authHeaders() {
  const token = localStorage.getItem("skkkk_token");
  return token ? { Authorization: "Bearer " + token } : {};
}
function apiDetail(text) {
  try {
    const j = JSON.parse(text);
    const d = j.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join("; ");
    return text;
  } catch (_) { return text || "ошибка запроса"; }
}
async function postJson(url) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json;charset=utf-8", ...authHeaders() },
  });
  const text = await res.text();
  if (!res.ok) {
    const err = new Error(apiDetail(text));
    err.status = res.status;
    throw err;
  }
  return text;
}
const rebuildBtn = document.getElementById("rebuild-summary");
if (rebuildBtn) {
  rebuildBtn.addEventListener("click", async () => {
    const id = document.body.getAttribute("data-artifact-id");
    if (!id) return;
    rebuildBtn.disabled = true;
    rebuildBtn.setAttribute("aria-busy", "true");
    rebuildBtn.innerHTML = '<span class="action-spinner" aria-hidden="true"></span>Собираем…';
    try {
      await postJson("/api/artifacts/" + id + "/rebuild-summary");
      location.reload();
    } catch (err) {
      alert(err.message || "Не удалось пересобрать выжимку");
      rebuildBtn.disabled = false;
      rebuildBtn.removeAttribute("aria-busy");
      rebuildBtn.textContent = rebuildBtn.dataset.idleLabel || "Пересобрать выжимку";
    }
  });
}
</script>
"""


def _source_section(text: str) -> str:
    source = text.strip() or "Текст файла пуст."
    return (
        '<details class="source-collapse" id="doc-source">'
        '<summary class="source-summary">'
        '<span class="reg-toggle" aria-hidden="true">▸</span>'
        "<strong>Полный текст</strong>"
        '<span class="source-hint source-hint-open">(развернуть)</span>'
        '<span class="source-hint source-hint-close">(свернуть)</span>'
        "</summary>"
        f'<div class="source-body"><pre>{escape(source)}</pre></div>'
        "</details>"
    )
