from __future__ import annotations

import json
from html import escape
from pathlib import Path

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.services.entities import EntitiesPayload
from app.app.services.plain_text import clean_summary_output, has_llm_markup_artifacts, plainify_llm_markup
from app.app.services.storage import parsed_dir, structured_dir


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
) -> Path:
    path = document_report_path(settings, artifact)
    html = _render_document_report(artifact, payload, text)
    path.write_text(html, encoding="utf-8")
    return path


def ensure_document_report(settings: Settings, artifact: Artifact) -> Path:
    path = document_report_path(settings, artifact)
    if path.is_file():
        body = path.read_text(encoding="utf-8")
        if not has_llm_markup_artifacts(body):
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


def _render_document_report(artifact: Artifact, payload: EntitiesPayload, text: str) -> str:
    title = escape(artifact.original_filename)
    summary = clean_summary_output((payload.summary or "").strip())
    decisions = [plainify_llm_markup(d.strip()) for d in payload.decisions if d.strip()]
    tasks = [t for t in payload.tasks if t.description.strip()]
    facts = [
        (plainify_llm_markup(str(k).strip()), plainify_llm_markup(str(v).strip()))
        for k, v in payload.facts.items()
        if str(k).strip() and str(v).strip()
    ]
    participants = [p for p in payload.participants if p.name.strip()]
    text = plainify_llm_markup(text or "")

    summary_html = _section("Итоговая выжимка", f"<p>{escape(summary or 'Краткая выжимка не сформирована.')}</p>")
    decisions_html = _list_section("Ключевые решения", decisions, empty="Решения не выделены.")
    tasks_html = _tasks_section(tasks)
    facts_html = _facts_section(facts)
    participants_html = _participants_section(participants)
    source_html = _source_section(text)

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Отчет — {title}</title>
  <style>
    body {{
      font-family: Arial, sans-serif;
      margin: 24px;
      color: #212529;
      background: #fff;
      line-height: 1.5;
    }}
    h1, h2 {{
      margin: 0 0 12px;
    }}
    h1 {{
      font-size: 28px;
    }}
    h2 {{
      font-size: 22px;
      margin-top: 28px;
    }}
    .meta {{
      color: #6c757d;
      margin-bottom: 20px;
    }}
    .card {{
      border: 1px solid #dee2e6;
      border-radius: 8px;
      padding: 16px 18px;
      margin-bottom: 18px;
      background: #fff;
    }}
    ul {{
      margin: 0;
      padding-left: 22px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      border: 1px solid #dee2e6;
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #f8f9fa;
      width: 220px;
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      font-family: Consolas, monospace;
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <h1>Документ — {title}</h1>
  <div class="meta">Артефакт: {escape(artifact.artifact_id)}</div>
  {summary_html}
  {decisions_html}
  {tasks_html}
  {facts_html}
  {participants_html}
  {source_html}
</body>
</html>
"""


def _section(title: str, body: str) -> str:
    return f'<section class="card"><h2>{escape(title)}</h2>{body}</section>'


def _list_section(title: str, items: list[str], *, empty: str) -> str:
    if not items:
        return _section(title, f"<p>{escape(empty)}</p>")
    rows = "".join(f"<li>{escape(item)}</li>" for item in items)
    return _section(title, f"<ul>{rows}</ul>")


def _tasks_section(tasks: list[object]) -> str:
    if not tasks:
        return _section("Задачи", "<p>Задачи не выделены.</p>")
    rows = "".join(
        "<tr>"
        f"<td>{escape(plainify_llm_markup(getattr(task, 'description', '') or '-'))}</td>"
        f"<td>{escape(plainify_llm_markup(getattr(task, 'assignee', None) or '-'))}</td>"
        f"<td>{escape(plainify_llm_markup(getattr(task, 'deadline', None) or '-'))}</td>"
        f"<td>{escape(plainify_llm_markup(getattr(task, 'status', '') or '-'))}</td>"
        "</tr>"
        for task in tasks
    )
    return _section(
        "Задачи",
        (
            "<table><thead><tr><th>Описание</th><th>Ответственный</th>"
            "<th>Срок</th><th>Статус</th></tr></thead><tbody>"
            f"{rows}</tbody></table>"
        ),
    )


def _facts_section(facts: list[tuple[str, str]]) -> str:
    if not facts:
        return _section("Факты", "<p>Структурированные факты не выделены.</p>")
    rows = "".join(f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>" for key, value in facts)
    return _section("Факты", f"<table><tbody>{rows}</tbody></table>")


def _participants_section(participants: list[object]) -> str:
    if not participants:
        return _section("Участники", "<p>Участники не выделены.</p>")
    rows = "".join(
        f"<li>{escape(plainify_llm_markup(getattr(person, 'name', '') or '-'))} — "
        f"{escape(plainify_llm_markup(getattr(person, 'role', None) or 'роль не указана'))}</li>"
        for person in participants
    )
    return _section("Участники", f"<ul>{rows}</ul>")


def _source_section(text: str) -> str:
    source = text.strip() or "Пустой текст."
    return _section("Нормализованный текст", f"<pre>{escape(source)}</pre>")
