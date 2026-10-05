"""Разбор HTML и шаблонов: скрипты внутри разметки, элементы с id, обработчики-атрибуты, подключения шаблонов и связь с графом."""

from __future__ import annotations

from pathlib import Path

import pytest

from projectmap import grammars
from projectmap.build import build_graph, file_id, symbol_id, ui_id
from projectmap.config import Config
from projectmap.extract import extract_file
from projectmap.lang_html import neutralize_template_syntax

ROOT = Path(__file__).resolve().parents[3]

PAGE = '''{% extends "base.html" %}
{% include "partials/card.html" %}
<div id="box" class="x">
  <button id="save-btn" onclick="saveItem(this)">Сохранить</button>
  <a id="link" href="#" onclick="return app.open('x')">Открыть</a>
  <input id="name" placeholder="Название">
  <div id="{{ dynamic }}">пропускается</div>
</div>
<script src="/static/lib.js"></script>
<script src="https://cdn.example.com/x.js"></script>
<script type="application/json">{"a": 1}</script>
<script>
  const label = "{{ title }}";
  {% if flag %}
  const flagged = 1;
  {% endif %}
  function saveItem(el) {
    helper();
    document.getElementById('name').value = '';
  }
  async function loadAll() {
    await fetch('/api/items');
  }
  loadAll();
</script>
'''


@pytest.fixture(scope="module", autouse=True)
def grammars_ready():
    try:
        status = grammars.ensure(ROOT, ["javascript"])
    except grammars.GrammarUnavailable as exc:
        pytest.skip(str(exc))
    if "javascript" not in status.ready:
        pytest.skip("нет грамматики javascript")
    patch = pytest.MonkeyPatch()
    patch.setenv(grammars.CACHE_ENV, str(grammars.configure(ROOT)))
    yield
    patch.undo()


def test_scripts_inside_the_page_become_symbols_and_calls_with_file_lines():
    facts = extract_file(ROOT, "templates/page.html", PAGE)
    lines = PAGE.splitlines()
    symbols = {s.qualname: s for s in facts.symbols if s.kind != "module"}
    assert set(symbols) == {"saveItem", "loadAll"}
    assert lines[symbols["saveItem"].line - 1].strip().startswith("function saveItem"), "строка символа — строка файла, не фрагмента"
    calls = {(c.caller, c.callee, c.qualifier) for c in facts.calls}
    assert {("saveItem", "helper", ""), ("loadAll", "fetch", ""), ("<module>", "loadAll", "")} <= calls


def test_elements_with_ids_keep_labels_and_handlers_and_skip_dynamic_ids():
    facts = extract_file(ROOT, "templates/page.html", PAGE)
    elements = {e.id: e for e in facts.elements}
    assert set(elements) == {"box", "save-btn", "link", "name"}
    assert elements["save-btn"].handlers == ["saveItem"] and elements["save-btn"].label == "Сохранить" and elements["save-btn"].tag == "button"
    assert elements["link"].handlers == ["app.open"] and elements["name"].label == "Название"
    assert PAGE.splitlines()[elements["save-btn"].line - 1].strip().startswith('<button id="save-btn"')


def test_template_includes_and_local_script_sources_are_imports():
    facts = extract_file(ROOT, "templates/page.html", PAGE)
    assert {i.module for i in facts.imports} == {"base.html", "partials/card.html", "/static/lib.js"}


def test_template_syntax_inside_scripts_is_neutralised_without_moving_lines():
    script = 'const a = "{{ x }}";\n{% if y %}\nconst b = {{ z }};\n{% endif %}\n'
    clean = neutralize_template_syntax(script)
    assert clean.count("\n") == script.count("\n") and "{{" not in clean and "{%" not in clean


def test_page_without_scripts_or_elements_is_just_a_module():
    facts = extract_file(ROOT, "t.html", "<p>текст</p>\n")
    assert [s.kind for s in facts.symbols] == ["module"] and not facts.elements and not facts.calls


def test_graph_links_elements_to_handlers_includes_and_id_users(tmp_path):
    (tmp_path / "templates" / "partials").mkdir(parents=True)
    (tmp_path / "static").mkdir()
    (tmp_path / "templates" / "page.html").write_text(PAGE, encoding="utf-8")
    (tmp_path / "templates" / "base.html").write_text("<html></html>\n", encoding="utf-8")
    (tmp_path / "templates" / "partials" / "card.html").write_text('<div id="card">k</div>\n', encoding="utf-8")
    (tmp_path / "static" / "lib.js").write_text("function helper() {}\n", encoding="utf-8")
    patch = pytest.MonkeyPatch()
    patch.setenv(grammars.CACHE_ENV, str(grammars.configure(ROOT)))
    try:
        graph = build_graph(tmp_path, Config())
    finally:
        patch.undo()
    edges = {(e["from"], e["to"], e["rel"]): e for e in graph["edges"]}
    nodes = {n["id"] for n in graph["nodes"]}
    assert ui_id("templates/page.html", "save-btn") in nodes and ui_id("templates/partials/card.html", "card") in nodes
    page = "templates/page.html"
    assert (ui_id(page, "save-btn"), symbol_id(page, "saveItem"), "handled_by") in edges
    assert (ui_id(page, "name"), symbol_id(page, "saveItem"), "handled_by") in edges, "getElementById внутри функции связывает элемент с ней"
    assert (file_id(page), file_id("templates/base.html"), "imports") in edges and (file_id(page), file_id("static/lib.js"), "imports") in edges
    assert (symbol_id(page, "saveItem"), symbol_id("static/lib.js", "helper"), "calls") in edges, "вызов из скрипта шаблона в подключённый файл"
