"""Адаптер связи экрана с сервером: обращения скриптов к адресам API (`calls_api`) и шаблоны, которые отдаёт серверный код (`renders`).

Адрес берётся из первого аргумента `fetch(...)`, `api(...)`, `apiJson(...)`, `X.api(...)`, `axios.get(...)`, `$.post(...)`: строка, шаблонная
строка (`${id}` -> `{}`) или конкатенация (`'/p/' + id + '/x'`). Он сопоставляется с узлами `route` по шаблону пути; если метод назван рядом
(`method: 'POST'`, `axios.post`), берётся только он. Серверный код, возвращающий шаблон (`TemplateResponse`, `render_template`), связан с файлом
шаблона связью `renders`.
"""

from __future__ import annotations

import re

from projectmap.adapters import Adapter, AdapterContext
from projectmap.graphbuf import file_id, symbol_id
from projectmap.paths import is_test_path

WEB_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".html", ".htm")
CALL_HEAD = re.compile(r"(?:\bfetch|\b(?:[A-Za-z_$][\w$]*\.)?api(?:Json|Fetch|Request)?|\baxios(?:\.(?P<axios>get|post|put|delete|patch))?"
                       r"|\$\.(?P<jq>get|post|getJSON|ajax))\s*\(")
METHOD_HINT = re.compile(r"method\s*:\s*['\"](GET|POST|PUT|PATCH|DELETE)['\"]", re.I)
RENDERS = re.compile(r"(?:TemplateResponse|render_template|render_to_string|get_template)\(\s*(?:request\s*,\s*)?(?:name\s*=\s*)?['\"]([\w./\-]+\.(?:html|htm|jinja|j2))['\"]")
TEMPLATE_EXPRESSION = re.compile(r"\$\{[^}]*\}|\{\{.*?\}\}|\{[^}/]*\}")


def normalize_url(url: str) -> str:
    """`/p/18/x?y=1`, `/p/{id}/x`, `/p/${id}/x` — один шаблон `/p/{}/x`."""
    url = url.split("?", 1)[0]
    url = TEMPLATE_EXPRESSION.sub("{}", url)
    url = re.sub(r"/\d+(?=/|$)", "/{}", url)
    return url.rstrip("/") or "/"


def _argument(text: str, start: int) -> str:
    """Текст первого аргумента вызова: от позиции после `(` до запятой или закрывающей скобки верхнего уровня."""
    depth, quote, i = 0, "", start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    return text[start:i].strip()


def _call_text(text: str, start: int) -> str:
    """Весь текст вызова: от позиции после `(` до парной закрывающей скобки."""
    depth, quote, i = 0, "", start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        i += 1
    return text[start:i]


def _url_of(argument: str) -> str | None:
    """Адрес из аргумента: строка, шаблонная строка или конкатенация; неизвестные части становятся `{}`."""
    parts = re.split(r"\+(?=(?:[^'\"`]*['\"`][^'\"`]*['\"`])*[^'\"`]*$)", argument)
    built = ""
    for part in parts:
        part = part.strip()
        if len(part) >= 2 and part[0] == part[-1] and part[0] in "'\"`":
            built += part[1:-1]
        else:
            built += "{}"
    return built if built.startswith("/") else None


TEST_CLIENT_CALL = re.compile(r"\.(get|post|put|patch|delete)\(\s*f?['\"](/[^'\"]*)['\"]")


def _http_tests(ctx: AdapterContext, routes: dict[str, list[dict]]) -> int:
    """Тест обращается к адресу через тестовый клиент (`client.get("/x")`): он покрывает функцию-обработчик этого роута."""
    handlers = {e["from"]: e["to"] for e in ctx.edges.items.values() if e["rel"] == "handles"}
    count = 0
    for path in ctx.paths(".py"):
        if not is_test_path(path):
            continue
        text = ctx.text(path)
        if not text or not routes:
            continue
        for match in TEST_CLIENT_CALL.finditer(text):
            method, url = match.group(1).upper(), match.group(2)
            candidates = routes.get(normalize_url(url)) or routes.get(normalize_url("/api" + url)) or []
            line = text.count("\n", 0, match.start()) + 1
            for route in [r for r in candidates if r["sub"] == method] or ([] if method else candidates):
                if route["id"] in handlers:
                    ctx.edges.add(ctx.owner_of(path, line), handlers[route["id"]], "covers", line, how="http")
                    count += 1
    return count


def _applies(ctx: AdapterContext) -> bool:
    return any(n["kind"] == "route" for n in ctx.nodes.values()) or any(p.endswith(".py") for p in ctx.facts)


def _run(ctx: AdapterContext) -> None:
    routes: dict[str, list[dict]] = {}
    for node in ctx.nodes.values():
        if node["kind"] == "route":
            routes.setdefault(normalize_url(node["label"].split(" ", 1)[1]), []).append(node)
    linked = 0
    for path in ctx.paths(*WEB_SUFFIXES):
        text = ctx.text(path)
        if not text or not routes:
            continue
        for match in CALL_HEAD.finditer(text):
            url = _url_of(_argument(text, match.end()))
            if not url:
                continue
            key = normalize_url(url)
            candidates = routes.get(key) or routes.get(normalize_url("/api" + url)) or []
            hint = (match.group("axios") or match.group("jq") or "").upper()
            hinted = METHOD_HINT.search(_call_text(text, match.end()))
            method = hint if hint in ("GET", "POST", "PUT", "PATCH", "DELETE") else (hinted.group(1).upper() if hinted else "")
            chosen = [r for r in candidates if r["sub"] == method] if method else candidates
            line = text.count("\n", 0, match.start()) + 1
            for route in chosen or []:
                ctx.edges.add(ctx.owner_of(path, line), route["id"], "calls_api", line)
                linked += 1
    covered = _http_tests(ctx, routes)
    rendered = 0
    for path in ctx.paths(".py"):
        text = ctx.text(path)
        if not text or "template" not in text.lower():
            continue
        for match in RENDERS.finditer(text):
            files = ctx.resolver.index.find_suffix(match.group(1))
            if len(files) == 1:
                line = text.count("\n", 0, match.start()) + 1
                ctx.edges.add(ctx.owner_of(path, line), file_id(files[0]), "renders", line)
                rendered += 1
    ctx.notes.append(f"экран → API: обращений {linked}; шаблонов, которые отдаёт сервер: {rendered}; тесты, ходящие в роуты: {covered}")


ADAPTER = Adapter("web", "Экран → API и шаблоны", _applies, _run)
