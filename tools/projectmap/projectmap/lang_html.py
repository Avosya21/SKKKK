"""Разбор HTML и шаблонов (Jinja и подобных): скрипты внутри `<script>`, элементы с `id`, обработчики-атрибуты, подключения шаблонов.

В таких проектах логика интерфейса часто живёт внутри самих шаблонов, поэтому скрипты разбираются как JavaScript: функции,
вызовы и импорты принадлежат файлу шаблона, строки пересчитываются в строки файла. Конструкции шаблонизатора внутри скрипта
(`{{ x }}`, `{% if %}`) заменяются пробелами и нулём, чтобы не ломать разбор и не сдвигать строки. Подключения `{% extends %}`,
`{% include %}`, `{% import %}` и `<script src>` становятся импортами файла.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from projectmap.lang_specs import spec_for
from projectmap.lang_treesitter import extract_treesitter
from projectmap.languages import BY_ID, Language
from projectmap.model import Element, FileFacts, Import, Symbol

JS_TYPES = {"", "text/javascript", "application/javascript", "module", "text/babel"}
LABEL_TAGS = frozenset({"button", "a", "label", "summary", "legend", "h1", "h2", "h3", "h4", "h5", "h6", "th", "option", "span", "div", "li"})
HANDLER = re.compile(r"^\s*(?:return\s+)?(?:await\s+)?([A-Za-z_$][\w$.]*)\s*\(")
JINJA_EXPRESSION = re.compile(r"\{\{.*?\}\}", re.S)
JINJA_BLOCK = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.S)
JINJA_IMPORT = re.compile(r"\{%-?\s*(?:extends|include|import|from)\s+['\"]([^'\"]+)['\"]")


def _blank(match: re.Match[str], fill: str) -> str:
    """Замена с сохранением переводов строк: число строк скрипта не меняется."""
    text = match.group(0)
    return fill + "".join("\n" if ch == "\n" else " " for ch in text[len(fill):]) if len(text) >= len(fill) else fill


def neutralize_template_syntax(script: str) -> str:
    script = JINJA_EXPRESSION.sub(lambda m: _blank(m, "0"), script)
    return JINJA_BLOCK.sub(lambda m: _blank(m, ""), script)


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[tuple[int, str]] = []  # (строка тега, текст скрипта)
        self.script_sources: list[tuple[int, str]] = []
        self.elements: list[Element] = []
        self._script_line: int | None = None
        self._script_buffer: list[str] = []
        self._open_elements: list[Element] = []
        self.path = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        line = self.getpos()[0]
        if tag == "script":
            if values.get("src"):
                self.script_sources.append((line, values["src"]))
            elif values.get("type", "").lower() in JS_TYPES:
                self._script_line, self._script_buffer = line, []
        element_id = values.get("id", "")
        handlers = [m.group(1) for k, v in values.items() if k.startswith("on") and (m := HANDLER.match(v))]
        if element_id and "{{" not in element_id and "{%" not in element_id:
            label = values.get("aria-label") or values.get("title") or values.get("placeholder") or values.get("value") or ""
            element = Element(self.path, element_id, tag, line, label.strip()[:80], handlers)
            self.elements.append(element)
            if tag in LABEL_TAGS and not label:
                self._open_elements.append(element)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script_line is not None:
            self.scripts.append((self._script_line, "".join(self._script_buffer)))
            self._script_line = None
        if self._open_elements and self._open_elements[-1].tag == tag:
            self._open_elements.pop()

    def handle_data(self, data: str) -> None:
        if self._script_line is not None:
            self._script_buffer.append(data)
        elif self._open_elements and not self._open_elements[-1].label and data.strip():
            self._open_elements[-1].label = " ".join(data.split())[:80]


def extract_html(root: Path, path: str, text: str, language: Language) -> FileFacts:
    facts = FileFacts(path, language.id, language.tier, lines=len(text.splitlines()))
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    facts.symbols.append(Symbol(stem, path, "module", path, 1, max(facts.lines, 1), lang=language.id))
    for match in JINJA_IMPORT.finditer(text):
        facts.imports.append(Import(path, match.group(1), [], {}, text.count("\n", 0, match.start()) + 1))
    parser = _Page()
    parser.path = path
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - битая разметка не должна ронять разбор
        facts.error = f"разметка разобрана не полностью: {exc.__class__.__name__}"
    for line, source in parser.script_sources:
        if not source.startswith(("http://", "https://", "//", "{{", "data:")):
            facts.imports.append(Import(path, source.split("?", 1)[0], [], {}, line))
    facts.elements = parser.elements
    js = BY_ID["javascript"]
    spec = spec_for(js)
    for line, script in parser.scripts:
        if not script.strip() or spec is None:
            continue
        part = extract_treesitter(root, path, neutralize_template_syntax(script), js, spec, line_offset=line - 1, with_module=False)
        if part.error and not part.symbols and not part.calls and not part.imports and "грамматики" in part.error:
            facts.error = part.error
            break
        facts.symbols += part.symbols
        facts.calls += part.calls
        facts.imports += part.imports
    seen: set[tuple[str, int]] = set()
    facts.imports = [i for i in facts.imports if not ((i.module, i.line) in seen or seen.add((i.module, i.line)))]
    facts.calls.sort(key=lambda c: (c.line, c.caller, c.callee, c.qualifier))
    return facts
