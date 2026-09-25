"""Нарезка файлов проекта на фрагменты для индекса: Python, JS, шаблоны, CSS, Markdown, конфиги.

Один фрагмент — одна функция / метод / класс / блок шаблона / раздел документа, с адресом `путь:строка`.
Параллельно собираются таблицы символов, вызовов и импортов (для режимов --callers, --symbol и т.д.).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from app.app.services.project_index.config import redact

MAX_TEXT = 12000

PY_SKIP_CALLS = {
    "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple", "isinstance", "print", "range", "sorted",
    "enumerate", "zip", "min", "max", "sum", "any", "all", "getattr", "setattr", "hasattr", "open", "type", "super",
    "repr", "iter", "next", "map", "filter", "reversed", "abs", "round", "bytes", "frozenset", "id", "format",
}
JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "typeof", "new", "await", "async", "else",
               "do", "try", "finally", "throw", "delete", "void", "in", "of", "with", "super", "import", "export"}


@dataclass
class Chunk:
    path: str
    kind: str
    title: str
    symbol: str
    line_start: int
    line_end: int
    text: str
    meta: dict = field(default_factory=dict)


@dataclass
class Symbol:
    name: str
    qualname: str
    kind: str
    path: str
    line: int
    end_line: int
    signature: str = ""
    decorators: str = ""
    doc: str = ""
    lang: str = "py"


@dataclass
class Call:
    path: str
    caller: str
    callee: str
    qualifier: str
    line: int
    lang: str = "py"


@dataclass
class Import:
    path: str
    module: str
    names: str
    line: int


@dataclass
class FileResult:
    chunks: list[Chunk] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)


def _mk(path: str, kind: str, title: str, symbol: str, start: int, end: int, text: str, meta: dict | None = None,
        env_like: bool = False) -> Chunk:
    text = redact(text, env_like=env_like)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n…"
    return Chunk(path, kind, title, symbol, start, end, text, meta or {})


def _split_lines(path: str, kind: str, title: str, symbol: str, start_line: int, lines: list[str], max_lines: int,
                 meta: dict | None = None, header: str = "", env_like: bool = False) -> list[Chunk]:
    """Разрезать длинный фрагмент на части по max_lines, каждая с шапкой `путь:строки`."""
    out: list[Chunk] = []
    parts = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)] or [[]]
    for n, part in enumerate(parts, 1):
        s = start_line + (n - 1) * max_lines
        e = s + len(part) - 1
        suffix = f" (часть {n}/{len(parts)})" if len(parts) > 1 else ""
        head = f"{header}{path}:{s}-{e}{suffix}\n" if header != "" else ""
        out.append(_mk(path, kind, title + suffix, symbol, s, e, head + "\n".join(part), meta, env_like))
    return out


# ───────────────────────────── Python ─────────────────────────────

def _py_calls(rel: str, node: ast.AST, caller: str, known: set[str], out: list[Call]) -> None:
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            name, qual = func.id, ""
        elif isinstance(func, ast.Attribute):
            name = func.attr
            try:
                qual = ast.unparse(func.value)[:40]
            except Exception:  # noqa: BLE001
                qual = ""
        else:
            continue
        if name not in PY_SKIP_CALLS:
            out.append(Call(rel, caller, name, qual, getattr(sub, "lineno", 0)))
        # функции, переданные как значение: Depends(get_db), to_thread(fn), Thread(target=fn)
        refs = list(sub.args) + [kw.value for kw in sub.keywords]
        for arg in refs:
            if isinstance(arg, ast.Name) and arg.id in known and arg.id != name:
                out.append(Call(rel, caller, arg.id, "<ref>", getattr(sub, "lineno", 0)))


def chunk_python(rel: str, src: str, max_lines: int) -> FileResult:
    res = FileResult()
    lines = src.splitlines()
    is_test = "/tests/" in f"/{rel}" or rel.rsplit("/", 1)[-1].startswith("test_")
    kind_fn = "test" if is_test else "function"
    try:
        tree = ast.parse(src)
    except SyntaxError:
        res.chunks = _split_lines(rel, "code", rel, "", 1, lines, max_lines, header="")
        return res

    known: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            known.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            known.update(a.asname or a.name for a in node.names)

    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                res.imports.append(Import(rel, a.name, a.asname or "", node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = ("." * node.level) + (node.module or "")
            res.imports.append(Import(rel, module, ",".join(a.name for a in node.names), node.lineno))

    doc = ast.get_docstring(tree) or ""
    mod_name = rel.rsplit("/", 1)[-1].removesuffix(".py")
    res.symbols.append(Symbol(mod_name, rel, "module", rel, 1, len(lines), "", "", doc[:300]))
    for node in tree.body:  # константы модуля: LIVEKIT_TOKEN_TTL_HOURS = 4
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        if target and target.isupper() and len(target) > 2 and node.value is not None:
            res.symbols.append(Symbol(target, target, "constant", rel, node.lineno, node.end_lineno or node.lineno,
                                      lines[node.lineno - 1].strip()[:140], "", ""))

    # обзор модуля: докстринг + перечень определений (по нему находят файл по смыслу, а не по импортам)
    top_names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    consts = [s.name for s in res.symbols if s.kind == "constant"]
    doc_lines = doc.strip().splitlines()
    overview_lines = [f"{rel} — {doc_lines[0] if doc_lines else 'модуль без описания'}"]
    overview_lines += doc_lines[1:8]
    overview_lines.append(f"Определяет: {', '.join(top_names[:60]) or '—'}")
    if consts:
        overview_lines.append(f"Константы: {', '.join(consts[:20])}")
    overview = "\n".join(overview_lines)
    res.chunks.append(_mk(rel, "overview", rel, mod_name, 1, min(len(lines), 5), overview))

    def add_func(node: ast.FunctionDef | ast.AsyncFunctionDef, qual: str) -> None:
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = node.end_lineno or node.lineno
        body = lines[start - 1:end]
        decos = "; ".join(ast.unparse(d)[:80] for d in node.decorator_list)
        sig = lines[node.lineno - 1].strip()
        fdoc = ast.get_docstring(node) or ""
        kind = ("async " if isinstance(node, ast.AsyncFunctionDef) else "") + (kind_fn if "." not in qual else ("test" if is_test else "method"))
        res.symbols.append(Symbol(node.name, qual, kind, rel, start, end, sig, decos, fdoc[:300]))
        res.chunks.extend(_split_lines(rel, "test" if is_test else "function", qual, qual, start, body, max_lines,
                                       meta={"decorators": decos, "doc": fdoc[:200]}, header=""))
        # шапка `путь:строки` внутри text помогает FTS и выдаче
        for ch in res.chunks[-max(1, (len(body) + max_lines - 1) // max_lines):]:
            ch.text = f"{rel}:{ch.line_start}-{ch.line_end}  {qual}\n{ch.text}"
        _py_calls(rel, node, qual, known, res.calls)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add_func(node, node.name)
        elif isinstance(node, ast.ClassDef):
            end = node.end_lineno or node.lineno
            methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            first_m = min([m.lineno for m in methods] + [end + 1])
            head = lines[node.lineno - 1: max(node.lineno, min(first_m - 1, node.lineno + max_lines))]
            cdoc = ast.get_docstring(node) or ""
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            res.symbols.append(Symbol(node.name, node.name, "class", rel, node.lineno, end, f"class {node.name}({bases})",
                                      "; ".join(ast.unparse(d)[:80] for d in node.decorator_list), cdoc[:300]))
            res.chunks.append(_mk(rel, "class", node.name, node.name, node.lineno, node.lineno + len(head) - 1,
                                  f"{rel}:{node.lineno}  class {node.name}({bases}); методы: "
                                  f"{', '.join(m.name for m in methods)}\n" + "\n".join(head),
                                  meta={"methods": [m.name for m in methods]}))
            for m in methods:
                add_func(m, f"{node.name}.{m.name}")
            _py_calls(rel, ast.Module(body=[n for n in node.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))], type_ignores=[]),
                      f"{node.name}.<class>", known, res.calls)
    # код уровня модуля между определениями: константы, create_engine(...), регистрация роутеров и т.п.
    covered = [False] * (len(lines) + 2)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            lo = min([node.lineno] + [d.lineno for d in node.decorator_list])
            for k in range(lo, (node.end_lineno or node.lineno) + 1):
                covered[k] = True
    run: list[int] = []

    def flush_run() -> None:
        if run and any(lines[k - 1].strip() for k in run):
            body = [lines[k - 1] for k in run]
            for ch in _split_lines(rel, "module", f"{rel} · строки {run[0]}-{run[-1]}", mod_name, run[0], body, max_lines):
                ch.text = f"{rel}:{ch.line_start}-{ch.line_end}  (уровень модуля)\n{ch.text}"
                res.chunks.append(ch)
        run.clear()

    for k in range(1, len(lines) + 1):
        if covered[k]:
            flush_run()
        else:
            run.append(k)
    flush_run()
    # вызовы верхнего уровня модуля (не внутри def)
    top = ast.Module(body=[n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))], type_ignores=[])
    _py_calls(rel, top, "<module>", known, res.calls)
    return res


# ───────────────────────────── JavaScript ─────────────────────────────

_JS_FUNC = re.compile(r"^(\s*)(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(")
_JS_ASSIGN = re.compile(r"^(\s*)(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)")
_JS_METHOD = re.compile(r"^(\s{2,4})(?:async\s+)?(?:static\s+)?(?:get\s+|set\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{\s*$")
_JS_PROP = re.compile(r"^(\s{2,4})([A-Za-z_$][\w$]*)\s*:\s*(?:async\s*)?(?:function\b[^{]*|\([^)]*\)\s*=>)\s*\{\s*$")
_JS_CONTAINER = re.compile(r"^(?:export\s+)?(?:const|let|var|class)\s+([A-Za-z_$][\w$]*)\s*(?:=\s*\{|\{|extends\b.*\{)\s*$")
_JS_CALL = re.compile(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")
_JS_ENDPOINT = re.compile(r"""[`'"](/[A-Za-z][A-Za-z0-9_\-./${}?=&]*)[`'"]""")
_JS_STATIC_EXT = (".js", ".css", ".png", ".svg", ".jpg", ".ico", ".webmanifest", ".woff", ".woff2", ".html")


def _js_block_end(lines: list[str], start: int, limit: int = 1500) -> int:
    """Индекс (0-based) строки, где закрывается блок `{ … }`, начатый в строке start."""
    depth = 0
    seen = False
    in_block_comment = False
    quote = ""
    for i in range(start, min(len(lines), start + limit)):
        line = lines[i]
        j = 0
        while j < len(line):
            ch = line[j]
            two = line[j:j + 2]
            if in_block_comment:
                if two == "*/":
                    in_block_comment = False
                    j += 1
            elif quote:
                if ch == "\\":
                    j += 1
                elif ch == quote:
                    quote = ""
            else:
                if two == "//":
                    break
                if two == "/*":
                    in_block_comment = True
                    j += 1
                elif ch in "'\"`":
                    quote = ch
                elif ch == "{":
                    depth += 1
                    seen = True
                elif ch == "}":
                    depth -= 1
                    if seen and depth <= 0:
                        return i
            j += 1
        if quote in "'\"":  # строка не переносится на следующую строку
            quote = ""
        if seen and depth <= 0:
            return i
    return min(len(lines) - 1, start)


def _js_calls(rel: str, caller: str, body: str, first_line: int, out: list[Call], lang: str = "js") -> list[str]:
    endpoints: list[str] = []
    for m in _JS_ENDPOINT.finditer(body):
        url = m.group(1)
        if not url.lower().endswith(_JS_STATIC_EXT) and len(url) > 2:
            endpoints.append(url)
    for lineno, line in enumerate(body.splitlines(), first_line):
        for m in _JS_CALL.finditer(line):
            chain = m.group(1)
            name = chain.rsplit(".", 1)[-1]
            if name in JS_KEYWORDS or name == caller.rsplit(".", 1)[-1] and "." not in chain:
                continue
            qual = chain.rsplit(".", 1)[0] if "." in chain else ""
            out.append(Call(rel, caller, name, qual, lineno, lang))
    return sorted(set(endpoints))


def chunk_js(rel: str, src: str, max_lines: int, *, line_offset: int = 0, lang: str = "js") -> FileResult:
    res = FileResult()
    lines = src.splitlines()
    covered = [False] * len(lines)
    containers: list[tuple[int, int, str]] = []  # (start, end, name)
    i = 0
    while i < len(lines):
        line = lines[i]
        mc = _JS_CONTAINER.match(line)
        if mc:
            end = _js_block_end(lines, i)
            containers.append((i, end, mc.group(1)))
        i += 1

    def container_of(idx: int) -> str:
        best = ""
        for s, e, name in containers:
            if s < idx <= e:
                best = name
        return best

    i = 0
    while i < len(lines):
        if covered[i]:
            i += 1
            continue
        line = lines[i]
        name = ""
        m = _JS_FUNC.match(line) or _JS_ASSIGN.match(line)
        if m:
            name = m.group(2)
        else:
            m = _JS_METHOD.match(line) or _JS_PROP.match(line)
            if m and m.group(2) not in JS_KEYWORDS:
                name = m.group(2)
        if not name or not line.rstrip().endswith(("{", "=>", "(", ",")) and "{" not in line:
            i += 1
            continue
        end = _js_block_end(lines, i)
        # комментарии над функцией
        start = i
        while start > 0 and lines[start - 1].strip().startswith(("//", "/*", "*", "*/")) and i - start < 10:
            start -= 1
        owner = container_of(i)
        qual = f"{owner}.{name}" if owner else name
        body_lines = lines[start:end + 1]
        first = start + 1 + line_offset
        for k in range(start, end + 1):
            covered[k] = True
        endpoints = _js_calls(rel, qual, "\n".join(lines[i:end + 1]), i + 1 + line_offset, res.calls, lang)
        sig = line.strip()[:160]
        res.symbols.append(Symbol(name, qual, "function" if not owner else "method", rel, i + 1 + line_offset,
                                  end + 1 + line_offset, sig, "", "", lang))
        chs = _split_lines(rel, "js", qual, qual, first, body_lines, max_lines, meta={"endpoints": endpoints}, header="")
        for ch in chs:
            ch.text = f"{rel}:{ch.line_start}-{ch.line_end}  {qual}\n{ch.text}"
        res.chunks.extend(chs)
        i = end + 1

    # остальной код верхнего уровня
    run: list[int] = []
    def flush() -> None:
        if not run:
            return
        body = [lines[k] for k in run]
        if any(x.strip() for x in body):
            res.chunks.extend(_split_lines(rel, "js_top", "верхний уровень", "", run[0] + 1 + line_offset, body, max_lines, header=""))
        run.clear()
    for k, cov in enumerate(covered):
        if cov:
            flush()
        else:
            run.append(k)
    flush()
    return res


# ───────────────────────────── Шаблоны HTML / Jinja ─────────────────────────────

_TAG_ID = re.compile(r"<(\w+)\b([^<>]*?)\bid=\"([^\"]+)\"([^<>]*)>")
_ATTR = lambda name, s: (re.search(rf"\b{name}=\"([^\"]*)\"", s) or [None, ""])[1]  # noqa: E731


def chunk_template(rel: str, src: str, max_lines: int) -> FileResult:
    res = FileResult()
    lines = src.splitlines()
    # inline-скрипты: собственные фрагменты как JS
    script_ranges: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if re.search(r"<script\b(?![^>]*\bsrc=)[^>]*>", lines[i]) and "</script>" not in lines[i]:
            j = i + 1
            while j < len(lines) and "</script>" not in lines[j]:
                j += 1
            script_ranges.append((i + 1, j - 1))  # тело скрипта, 0-based включительно
            i = j
        i += 1
    covered = [False] * len(lines)
    for s, e in script_ranges:
        if e < s:
            continue
        sub = chunk_js(rel, "\n".join(lines[s:e + 1]), max_lines, line_offset=s, lang="js")
        for ch in sub.chunks:
            ch.kind = "js" if ch.kind == "js" else "js_top"
            ch.meta["inline"] = True
        res.chunks.extend(sub.chunks)
        res.symbols.extend(sub.symbols)
        res.calls.extend(sub.calls)
        for k in range(s, e + 1):
            covered[k] = True

    # элементы с id: кнопки, поля, панели
    for idx, line in enumerate(lines):
        if covered[idx]:
            continue
        for m in _TAG_ID.finditer(line):
            tag, pre, eid, post = m.groups()
            attrs = f"{pre} {post}"
            label = _ATTR("aria-label", attrs) or _ATTR("title", attrs) or _ATTR("placeholder", attrs)
            inner = re.sub(r"<[^>]+>", " ", line[m.end():]).strip()
            inner = re.sub(r"\s+", " ", inner)[:120]
            text = f"{rel}:{idx + 1}  <{tag} id=\"{eid}\">  {label}  {inner}\n{line.strip()[:400]}"
            res.chunks.append(_mk(rel, "ui_element", eid, eid, idx + 1, idx + 1, text,
                                  meta={"id": eid, "tag": tag, "label": label or inner}))

    # окна вёрстки по 60 строк, с именем ближайшего {% block %}
    window = 60
    block = ""
    k = 0
    while k < len(lines):
        if covered[k]:
            k += 1
            continue
        chunk_lines: list[str] = []
        s = k
        while k < len(lines) and not covered[k] and len(chunk_lines) < window:
            mb = re.search(r"{%\s*block\s+(\w+)", lines[k])
            if mb:
                block = mb.group(1)
            chunk_lines.append(lines[k])
            k += 1
        if any(x.strip() for x in chunk_lines):
            title = f"{rel.rsplit('/', 1)[-1]}" + (f" · block {block}" if block else "")
            res.chunks.append(_mk(rel, "template", title, block, s + 1, s + len(chunk_lines),
                                  f"{rel}:{s + 1}-{s + len(chunk_lines)}\n" + "\n".join(chunk_lines)))
    return res


# ───────────────────────────── CSS ─────────────────────────────

def chunk_css(rel: str, src: str, max_lines: int) -> FileResult:
    res = FileResult()
    lines = src.splitlines()
    depth = 0
    start = 0
    for i, line in enumerate(lines):
        depth += line.count("{") - line.count("}")
        at_boundary = depth <= 0 and line.strip().endswith("}")
        if at_boundary and i - start + 1 >= 30 or i == len(lines) - 1:
            body = lines[start:i + 1]
            if any(x.strip() for x in body):
                sel = next((x.strip().split("{")[0].strip() for x in body if "{" in x), "")
                res.chunks.append(_mk(rel, "css", sel[:80] or rel, sel[:80], start + 1, i + 1,
                                      f"{rel}:{start + 1}-{i + 1}\n" + "\n".join(body)))
            start = i + 1
            depth = 0
    return res


# ───────────────────────────── Markdown ─────────────────────────────

_MD_HEAD = re.compile(r"^(#{1,4})\s+(.*)$")
_MD_ROW = re.compile(r"^\|\s*(\d{1,3})\s*\|")


def chunk_markdown(rel: str, src: str, max_lines: int) -> FileResult:
    res = FileResult()
    lines = src.splitlines()
    sections: list[tuple[str, int, list[str]]] = []
    stack: list[tuple[int, str]] = []
    cur_title, cur_start, cur_lines = rel.rsplit("/", 1)[-1], 1, []
    in_fence = False
    for idx, line in enumerate(lines, 1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else _MD_HEAD.match(line)
        if m and len(m.group(1)) in (1, 2, 3):
            if any(x.strip() for x in cur_lines):
                sections.append((cur_title, cur_start, cur_lines))
            level, title = len(m.group(1)), m.group(2).strip()
            stack = [(lv, t) for lv, t in stack if lv < level] + [(level, title)]
            cur_title = " > ".join(t for _, t in stack)
            cur_start, cur_lines = idx, [line]
        else:
            cur_lines.append(line)
    if any(x.strip() for x in cur_lines):
        sections.append((cur_title, cur_start, cur_lines))

    finding_file = "FINDINGS" in rel
    for title, start, body in sections:
        rows: list[tuple[int, str]] = []
        rest: list[str] = []
        for off, line in enumerate(body):
            if finding_file and _MD_ROW.match(line):
                rows.append((start + off, line))
            else:
                rest.append(line)
        for lineno, row in rows:
            num = _MD_ROW.match(row).group(1)  # type: ignore[union-attr]
            res.chunks.append(_mk(rel, "finding", f"Находка {num}", f"finding-{num}", lineno, lineno,
                                  f"{rel}:{lineno}  Находка {num}\n{row}", meta={"number": int(num)}))
        text = "\n".join(rest).strip()
        if len(text) < 40:
            continue
        # длинные разделы — по абзацам, до ~2500 знаков
        buf: list[str] = []
        size = 0
        part = 1
        first_line = start
        def emit() -> None:
            nonlocal buf, size, part, first_line
            if buf:
                t = "\n".join(buf)
                res.chunks.append(_mk(rel, "doc", title + (f" (часть {part})" if part > 1 else ""), "", first_line,
                                      first_line + len(buf) - 1, f"{rel}:{first_line}  {title}\n{t}"))
                part += 1
                first_line += len(buf)
                buf, size = [], 0
        for line in rest:
            buf.append(line)
            size += len(line) + 1
            if size > 2500 and not line.strip():
                emit()
        emit()
    return res


# ───────────────────────────── Манифест, конфиги, PowerShell ─────────────────────────────

def chunk_manifest(rel: str, src: str) -> FileResult:
    import yaml

    res = FileResult()
    data = yaml.safe_load(src) or {}
    for node in data.get("nodes") or []:
        text = f"NODE {node.get('id')}\nkind={node.get('kind')} path={node.get('path')}\ntitle={node.get('title')}\ntags={node.get('tags')}"
        res.chunks.append(_mk(rel, "manifest_node", str(node.get("id")), str(node.get("id")), 1, 1, text,
                              meta={"node_path": node.get("path")}))
    for edge in data.get("edges") or []:
        text = f"EDGE {edge.get('from')} --[{edge.get('relation')}]--> {edge.get('to')}"
        res.chunks.append(_mk(rel, "manifest_edge", f"{edge.get('from')}->{edge.get('to')}", "", 1, 1, text))
    return res


def chunk_generic(rel: str, src: str, max_lines: int, kind: str = "config") -> FileResult:
    """Конфиги и скрипты: блоки, разделённые пустыми строками, до 60 строк; PowerShell — ещё и по функциям."""
    res = FileResult()
    lines = src.splitlines()
    env_like = rel.endswith((".env.example", ".ini")) or rel.rsplit("/", 1)[-1].startswith(".env")
    if rel.endswith(".ps1"):
        for i, line in enumerate(lines):
            m = re.match(r"^\s*function\s+([\w\-]+)", line)
            if m:
                res.symbols.append(Symbol(m.group(1), m.group(1), "function", rel, i + 1, i + 1, line.strip()[:120], lang="ps1"))
    blocks: list[tuple[int, list[str]]] = []
    cur: list[str] = []
    start = 1
    for idx, line in enumerate(lines, 1):
        if not line.strip() and cur and len(cur) >= 12:
            blocks.append((start, cur))
            cur, start = [], idx + 1
        else:
            if not cur:
                start = idx
            cur.append(line)
            if len(cur) >= 60:
                blocks.append((start, cur))
                cur, start = [], idx + 1
    if cur:
        blocks.append((start, cur))
    for s, body in blocks:
        if not any(x.strip() for x in body):
            continue
        title = next((x.strip("#;/ -").strip() for x in body if x.strip()), rel)[:80]
        first_sym = next((m.group(1) for x in body if (m := re.match(r"^\s*([A-Z][A-Z0-9_]{3,})\s*=", x))), "")
        res.chunks.append(_mk(rel, kind, f"{rel.rsplit('/', 1)[-1]} · {title}", first_sym, s, s + len(body) - 1,
                              f"{rel}:{s}-{s + len(body) - 1}\n" + "\n".join(body), env_like=env_like))
    return res


def chunk_file(rel: str, src: str, max_lines: int = 120) -> FileResult:
    name = rel.rsplit("/", 1)[-1]
    if rel.endswith(".py"):
        return chunk_python(rel, src, max_lines)
    if rel.endswith(".js"):
        return chunk_js(rel, src, max_lines)
    if rel.endswith(".html"):
        return chunk_template(rel, src, max_lines)
    if rel.endswith(".css"):
        return chunk_css(rel, src, max_lines)
    if rel.endswith((".md", ".mdc")):
        return chunk_markdown(rel, src, max_lines)
    if name == "manifest.yaml":
        return chunk_manifest(rel, src)
    return chunk_generic(rel, src, max_lines)
