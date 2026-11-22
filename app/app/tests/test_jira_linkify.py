"""Ссылки на задачи Jira в резюме и в ответе ассистента: рисует страница, текст в базе не меняется."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from app.app.services.jira_import import browse_base

ROOT = Path(__file__).resolve().parents[3]
APP_JS = ROOT / "app" / "app" / "static" / "app.js"
TEMPLATES = ROOT / "app" / "app" / "templates"
SPEC = {"base": "https://jira.example", "keys": ["ABC-1", "ABC-2", "1C_DEMO-2"]}


def _edge() -> Path:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise AssertionError("нужен Microsoft Edge, чтобы прогнать linkifyJiraKeys")


def _render(cases: list[dict]) -> list[str]:
    import tempfile

    edge = _edge()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        page = root / "page.html"
        profile = root / "profile"
        page.write_text(
            "<!DOCTYPE html><meta charset=\"utf-8\"><body><pre id=\"out\"></pre>"
            f"<script src=\"{APP_JS.as_uri()}\"></script><script>"
            f"const cases = {json.dumps(cases, ensure_ascii=True)};"
            "const out = cases.map((c) => PrimaryAnalitics.linkifyJiraKeys(c.html, c.spec));"
            "document.getElementById('out').textContent = JSON.stringify(out);"
            "</script></body>",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [str(edge), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={profile}", "--allow-file-access-from-files", "--dump-dom", page.as_uri()],
            capture_output=True, timeout=40, check=False,
        )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-2000:]
    dump = proc.stdout.decode("utf-8", "replace")
    start = dump.find('<pre id="out">')
    end = dump.find("</pre>", start)
    assert start != -1 and end != -1, dump[-1500:]
    import html as html_lib
    return json.loads(html_lib.unescape(dump[start + len('<pre id="out">'):end]))


def _anchor(shown: str, canon: str) -> str:
    label = f"Открыть {canon} в Jira"
    return (
        f'<a class="jira-issue-link" href="https://jira.example/browse/{canon}" target="_blank" '
        f'rel="noopener noreferrer" title="{label}" aria-label="{label}">{shown}</a>'
    )


def test_browse_base_keeps_only_a_plain_http_address():
    assert browse_base(SimpleNamespace(jira_base_url=" https://jira.example/ ")) == "https://jira.example"
    assert browse_base(SimpleNamespace(jira_base_url="http://jira.local/jira")) == "http://jira.local/jira"
    assert browse_base(SimpleNamespace(jira_base_url="")) == ""
    assert browse_base(SimpleNamespace(jira_base_url="javascript:alert(1)")) == ""
    assert browse_base(SimpleNamespace(jira_base_url='https://jira.example/"><script>')) == ""


def test_jira_link_is_blue_and_regular_weight():
    css = (ROOT / "app" / "app" / "static" / "design.css").read_text(encoding="utf-8")
    rule = css.split("a.jira-issue-link,", 1)[1].split("}", 1)[0]
    assert "color: var(--ds-link)" in rule
    assert "font-weight: 400" in rule
    assert ".table a.jira-issue-link:not(.btn)" in css
    assert "#tz-content a.jira-issue-link" in css and "#chat-log a.jira-issue-link" in css


def test_pages_linkify_summary_and_assistant():
    project = (TEMPLATES / "project.html").read_text(encoding="utf-8")
    chat = (TEMPLATES / "chat.html").read_text(encoding="utf-8")
    for html in (project, chat):
        assert "const jiraLinks = {{ jira_links | tojson }};" in html
        assert "PrimaryAnalitics.linkifyJiraKeys(safe, jiraLinks)" in html
    # резюме и ответ на странице проекта идут через inlineMarkdown; вопрос пользователя — нет
    assert project.index("function inlineMarkdown") < project.index("linkifyJiraKeys(safe, jiraLinks)")
    assert "escapeHtml(msg.content" in project


def test_known_issue_key_becomes_a_jira_link():
    rendered = _render([
        {"html": "Задача ABC-1.", "spec": SPEC},
        {"html": "abc-1 и 1C_DEMO-2", "spec": SPEC},
        {"html": "<strong>ABC-2</strong>", "spec": SPEC},
        {"html": "ГОСТ-2020 и XYZ-9 и 2026-11", "spec": SPEC},
        {"html": "<code>ABC-1</code>", "spec": SPEC},
        {"html": "см. https://jira.example/browse/ABC-1 дальше", "spec": SPEC},
        {"html": "xABC-1", "spec": SPEC},
        {"html": "ABC-1", "spec": {"base": "", "keys": ["ABC-1"]}},
        {"html": "ABC-1", "spec": {"base": "javascript:alert(1)", "keys": ["ABC-1"]}},
    ])
    assert rendered[0] == "Задача " + _anchor("ABC-1", "ABC-1") + "."
    assert rendered[1] == _anchor("abc-1", "ABC-1") + " и " + _anchor("1C_DEMO-2", "1C_DEMO-2")
    assert rendered[2] == "<strong>" + _anchor("ABC-2", "ABC-2") + "</strong>"
    assert rendered[3] == "ГОСТ-2020 и XYZ-9 и 2026-11"
    assert rendered[4] == "<code>ABC-1</code>"
    assert rendered[5] == "см. https://jira.example/browse/ABC-1 дальше"
    assert rendered[6] == "xABC-1"
    assert rendered[7] == "ABC-1"
    assert rendered[8] == "ABC-1"
    again = _render([{"html": rendered[0], "spec": SPEC}])[0]
    assert again == rendered[0]
