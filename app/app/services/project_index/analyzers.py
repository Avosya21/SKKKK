"""Структурные знания о проекте, которые нельзя надёжно получить из текста: роуты, БД, окружение,
связка «элемент интерфейса → JS → API», покрытие тестами, инварианты, тексты интерфейса, история git.

Всё выводится из кода (`ast`, регулярные выражения, git); руками ничего не пишется.
Результат — строки производных таблиц в индексе и «сгенерированные» фрагменты для поиска по смыслу.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
import subprocess
from pathlib import Path

from app.app.services.project_index.chunkers import Chunk
from app.app.services.project_index.config import ROOT

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
GUARD_CALL = re.compile(r"^_?(require|ensure|check|can|assert|verify)\w*", re.I)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


# ───────────────────────────── Роуты ─────────────────────────────

def analyze_routes(conn: sqlite3.Connection) -> list[Chunk]:
    conn.execute("DELETE FROM routes")
    prefixes: dict[str, str] = {}
    main_src = _read("app/app/main.py") if (ROOT / "app/app/main.py").is_file() else ""
    try:
        main_tree = ast.parse(main_src)
    except SyntaxError:
        main_tree = ast.Module(body=[], type_ignores=[])
    for node in ast.walk(main_tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "include_router":
            arg = node.args[0] if node.args else None
            prefix = next((_const_str(k.value) for k in node.keywords if k.arg == "prefix"), "") or ""
            if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                prefixes[arg.value.id] = prefix

    chunks: list[Chunk] = []
    files = sorted((ROOT / "app/app/routers").glob("*.py")) + [ROOT / "app/app/main.py"]
    for path in files:
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        router_prefix: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                fn = node.value.func
                if getattr(fn, "id", getattr(fn, "attr", "")) == "APIRouter" and node.targets and isinstance(node.targets[0], ast.Name):
                    router_prefix[node.targets[0].id] = next((_const_str(k.value) for k in node.value.keywords if k.arg == "prefix"), "") or ""
        module_alias = path.stem
        extra = prefixes.get(module_alias, "")
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in func.decorator_list:
                if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and deco.func.attr in HTTP_METHODS):
                    continue
                owner = deco.func.value.id if isinstance(deco.func.value, ast.Name) else ""
                url_tail = _const_str(deco.args[0]) if deco.args else ""
                if url_tail is None:
                    continue
                url = f"{extra}{router_prefix.get(owner, '')}{url_tail}" or "/"
                deps: list[str] = []
                for sub in ast.walk(func.args):
                    if isinstance(sub, ast.Call) and getattr(sub.func, "id", "") == "Depends" and sub.args:
                        deps.append(ast.unparse(sub.args[0]))
                for default in list(func.args.defaults) + [d for d in func.args.kw_defaults if d]:
                    if isinstance(default, ast.Call) and getattr(default.func, "id", "") == "Depends" and default.args:
                        deps.append(ast.unparse(default.args[0]))
                deps = list(dict.fromkeys(deps))
                guards: list[str] = []
                for sub in ast.walk(func):
                    if isinstance(sub, ast.Call):
                        name = getattr(sub.func, "id", getattr(sub.func, "attr", ""))
                        if GUARD_CALL.match(name):
                            guards.append(name)
                    elif isinstance(sub, ast.Compare) and "role" in ast.unparse(sub.left):
                        guards.append(ast.unparse(sub)[:60])
                for d in deps:
                    if GUARD_CALL.match(d):
                        guards.append(d)
                guards = list(dict.fromkeys(guards))
                method = deco.func.attr.upper()
                doc = ast.get_docstring(func) or ""
                conn.execute("INSERT INTO routes(method, url, handler, path, line, deps, guards, doc) VALUES (?,?,?,?,?,?,?,?)",
                             (method, url, func.name, rel, func.lineno, ", ".join(deps), ", ".join(guards), doc[:200]))
                text = (f"{method} {url} → {rel}:{func.lineno} {func.name}\nзависимости: {', '.join(deps) or '—'}\n"
                        f"проверки прав: {', '.join(guards) or '—'}\n{doc[:200]}")
                chunks.append(Chunk(rel, "route", f"{method} {url}", func.name, func.lineno, func.lineno, text,
                                    {"generated": True, "url": url, "method": method}))
    return chunks


# ───────────────────────────── База данных ─────────────────────────────

def analyze_db(conn: sqlite3.Connection) -> list[Chunk]:
    conn.execute("DELETE FROM db_columns")
    conn.execute("DELETE FROM table_access")
    class_table: dict[str, str] = {}
    tables: dict[str, list[tuple[str, str]]] = {}
    for path in sorted((ROOT / "app/app/models").glob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            tname = None
            for stmt in cls.body:
                if isinstance(stmt, ast.Assign) and stmt.targets and getattr(stmt.targets[0], "id", "") == "__tablename__":
                    tname = _const_str(stmt.value)
            if not tname:
                continue
            class_table[cls.name] = tname
            tables.setdefault(tname, [])
            for stmt in cls.body:
                target = value = ann = None
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    target, value, ann = stmt.target.id, stmt.value, ast.unparse(stmt.annotation)
                elif isinstance(stmt, ast.Assign) and stmt.targets and isinstance(stmt.targets[0], ast.Name):
                    target, value = stmt.targets[0].id, stmt.value
                if not target or target.startswith("__") or not isinstance(value, ast.Call):
                    continue
                fname = getattr(value.func, "id", getattr(value.func, "attr", ""))
                if fname not in {"Column", "mapped_column"}:
                    continue
                ctype = ann or (ast.unparse(value.args[0]) if value.args else "")
                conn.execute("INSERT INTO db_columns(tbl, col, ctype, source, path, line) VALUES (?,?,?,?,?,?)",
                             (tname, target, ctype[:80], "model", rel, stmt.lineno))
                tables[tname].append((target, ctype[:60]))
    # миграции без Alembic
    db_py = ROOT / "app/app/db.py"
    if db_py.is_file():
        try:
            tree = ast.parse(db_py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
                    if getattr(tgt, "id", "") == "_MIGRATIONS" and isinstance(node.value, ast.List):
                        for el in node.value.elts:
                            if isinstance(el, ast.Tuple) and len(el.elts) >= 3:
                                t, c, d = (_const_str(x) or "" for x in el.elts[:3])
                                conn.execute("INSERT INTO db_columns(tbl, col, ctype, source, path, line) VALUES (?,?,?,?,?,?)",
                                             (t, c, d, "migration", "app/app/db.py", el.lineno))
                                tables.setdefault(t, []).append((c, f"{d} (миграция)"))
        except SyntaxError:
            pass

    # кто обращается к таблицам: по классам моделей и по строкам SQL
    model_names = set(class_table)
    for row in conn.execute("SELECT DISTINCT path FROM symbols WHERE lang='py'").fetchall():
        rel = row[0]
        if "/models/" in rel or "/tests/" in rel or rel.startswith("scripts/") and False:
            continue
        try:
            tree = ast.parse(_read(rel))
        except (SyntaxError, OSError):
            continue
        scopes: list[tuple[str, ast.AST]] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scopes.append((node.name, node))
            elif isinstance(node, ast.ClassDef):
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        scopes.append((f"{node.name}.{m.name}", m))
        for qual, node in scopes:
            used: dict[str, int] = {}
            writes: set[str] = set()
            sql_write = False
            annotation_ids: set[int] = set()
            for sub in ast.walk(node):
                anns = []
                if isinstance(sub, ast.arg) and sub.annotation is not None:
                    anns.append(sub.annotation)
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.returns is not None:
                    anns.append(sub.returns)
                if isinstance(sub, ast.AnnAssign):
                    anns.append(sub.annotation)
                for ann in anns:
                    annotation_ids.update(id(x) for x in ast.walk(ann))
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and n.id in model_names and id(n) not in annotation_ids}
            calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
            call_names = {getattr(c.func, "attr", getattr(c.func, "id", "")) for c in calls}
            persists = bool(call_names & {"commit", "flush"})
            attr_store = any(isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) for n in ast.walk(node))
            constructed = {getattr(c.func, "id", "") for c in calls} & model_names
            dml = {arg.id for c in calls if getattr(c.func, "id", getattr(c.func, "attr", "")) in {"update", "insert", "delete"}
                   for arg in c.args if isinstance(arg, ast.Name) and arg.id in model_names}
            for name in names:
                used[class_table[name]] = node.lineno
                if name in constructed or name in dml or (persists and attr_store) or (call_names & {"add", "add_all", "merge", "delete"} and name in constructed):
                    writes.add(class_table[name])
            for const in ast.walk(node):
                if isinstance(const, ast.Constant) and isinstance(const.value, str) and len(const.value) > 8:
                    low = const.value.lower()
                    if not any(k in low for k in ("select ", "insert ", "update ", "delete ", "from ", "into ", "pragma")):
                        continue
                    for tname in tables:
                        if re.search(rf"\b{re.escape(tname)}\b", low):
                            used[tname] = const.lineno
                            if any(k in low for k in ("insert ", "update ", "delete ")):
                                writes.add(tname)
                                sql_write = True
            for tname, line in used.items():
                conn.execute("INSERT INTO table_access(tbl, path, symbol, line, writes) VALUES (?,?,?,?,?)",
                             (tname, rel, qual, line, 1 if tname in writes else 0))
    chunks: list[Chunk] = []
    for tname, cols in sorted(tables.items()):
        acc = conn.execute("SELECT path, symbol, writes FROM table_access WHERE tbl=?", (tname,)).fetchall()
        readers = sorted({f"{a[0]}::{a[1]}" for a in acc if not a[2]})[:12]
        writers = sorted({f"{a[0]}::{a[1]}" for a in acc if a[2]})[:12]
        text = (f"Таблица {tname}\nстолбцы: {', '.join(f'{c}:{t}' if t else c for c, t in cols)}\n"
                f"пишут: {', '.join(writers) or '—'}\nчитают: {', '.join(readers) or '—'}")
        first = conn.execute("SELECT path, line FROM db_columns WHERE tbl=? ORDER BY CASE source WHEN 'model' THEN 0 ELSE 1 END, line LIMIT 1", (tname,)).fetchone()
        chunks.append(Chunk(first[0] if first else "app/app/models", "dbtable", tname, tname, first[1] if first else 1,
                            first[1] if first else 1, text, {"generated": True}))
    return chunks


# ───────────────────────────── Окружение ─────────────────────────────

def analyze_env(conn: sqlite3.Connection) -> list[Chunk]:
    conn.execute("DELETE FROM env_vars")
    documented: set[str] = set()
    example = ROOT / ".env.example"
    if example.is_file():
        for line in example.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\s*#?\s*([A-Z][A-Z0-9_]{2,})\s*=", line)
            if m:
                documented.add(m.group(1))
    fields: dict[str, tuple[str, int]] = {}
    cfg = ROOT / "app/app/config.py"
    if cfg.is_file():
        tree = ast.parse(cfg.read_text(encoding="utf-8"))
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            for stmt in cls.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    default = ast.unparse(stmt.value)[:80] if stmt.value is not None else ""
                    fields[stmt.target.id] = (default, stmt.lineno)
        for name, (default, line) in fields.items():
            env = name.upper()
            conn.execute("INSERT INTO env_vars(name, default_value, path, line, how, documented) VALUES (?,?,?,?,?,?)",
                         (env, default, "app/app/config.py", line, "Settings", 1 if env in documented else 0))
    reads: dict[str, list[str]] = {}
    for row in conn.execute("SELECT DISTINCT path FROM symbols WHERE lang='py'").fetchall():
        rel = row[0]
        try:
            tree = ast.parse(_read(rel))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            name = default = None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                target = ast.unparse(node.func)
                if target in {"os.getenv", "os.environ.get", "environ.get"} and node.args:
                    name = _const_str(node.args[0])
                    default = ast.unparse(node.args[1])[:60] if len(node.args) > 1 else ""
            elif isinstance(node, ast.Subscript) and ast.unparse(node.value) in {"os.environ", "environ"}:
                name = _const_str(node.slice) if isinstance(node.slice, ast.Constant) else None
                default = ""
            if name:
                conn.execute("INSERT INTO env_vars(name, default_value, path, line, how, documented) VALUES (?,?,?,?,?,?)",
                             (name, default, rel, node.lineno, "os.environ", 1 if name in documented else 0))
                reads.setdefault(name, []).append(f"{rel}:{node.lineno}")
    for name in sorted(documented):
        if not conn.execute("SELECT 1 FROM env_vars WHERE name=?", (name,)).fetchone():
            conn.execute("INSERT INTO env_vars(name, default_value, path, line, how, documented) VALUES (?,?,?,?,?,?)",
                         (name, "", ".env.example", 0, "example-only", 1))
    chunks: list[Chunk] = []
    for row in conn.execute("SELECT name, MAX(default_value) d, MAX(documented) doc FROM env_vars GROUP BY name ORDER BY name").fetchall():
        where = conn.execute("SELECT path, line, how FROM env_vars WHERE name=? ORDER BY how", (row["name"],)).fetchall()
        text = (f"Переменная окружения {row['name']}; по умолчанию: {row['d'] or '—'}; "
                f"описана в .env.example: {'да' if row['doc'] else 'НЕТ'}\n" +
                "\n".join(f"  {w['how']}: {w['path']}:{w['line']}" for w in where[:8]))
        first = where[0]
        chunks.append(Chunk(first["path"], "env", row["name"], row["name"], first["line"] or 1, first["line"] or 1, text, {"generated": True}))
    return chunks


# ───────────────────────────── Элементы интерфейса → JS → API ─────────────────────────────

def analyze_ui(conn: sqlite3.Connection) -> list[Chunk]:
    conn.execute("DELETE FROM ui_links")
    conn.execute("DELETE FROM ui_text")
    elements = conn.execute("SELECT path, line_start, symbol, meta FROM chunks WHERE kind='ui_element'").fetchall()
    ids: dict[str, list[tuple[str, int, dict]]] = {}
    for el in elements:
        meta = json.loads(el["meta"] or "{}")
        ids.setdefault(el["symbol"], []).append((el["path"], el["line_start"], meta))
    js_chunks = conn.execute("SELECT path, symbol, line_start, text, meta FROM chunks WHERE kind IN ('js','js_top')").fetchall()
    token = re.compile(r"""["'`#]([A-Za-z][\w\-]{3,})["'`\s.,)\]]""")
    refs: dict[str, list[tuple[str, int, str, str]]] = {}
    for ch in js_chunks:
        meta = json.loads(ch["meta"] or "{}")
        ep = ", ".join(meta.get("endpoints", [])[:6])
        found = set(token.findall(ch["text"]))
        for eid in found & set(ids):
            refs.setdefault(eid, []).append((ch["path"], ch["line_start"], ch["symbol"], ep))
        # элементы, создаваемые JS
        for m in re.finditer(r"""\.id\s*=\s*["']([^"']+)["']""", ch["text"]):
            eid = m.group(1)
            refs.setdefault(eid, []).append((ch["path"], ch["line_start"], ch["symbol"], ep))
            ids.setdefault(eid, [(ch["path"], ch["line_start"], {"tag": "js-created", "label": ""})])
    for eid, defs in ids.items():
        for path, line, meta in defs:
            targets = refs.get(eid) or [("", 0, "", "")]
            for jp, jl, js, ep in targets[:6]:
                conn.execute("INSERT INTO ui_links(element_id, tag, label, path, line, js_path, js_line, js_symbol, endpoints) VALUES (?,?,?,?,?,?,?,?,?)",
                             (eid, meta.get("tag", ""), meta.get("label", ""), path, line, jp, jl, js, ep))
    # тексты интерфейса: русские строки в шаблонах и JS
    cyr = re.compile(r"[А-Яа-яЁё]")
    lit = re.compile(r"""["'`]([^"'`\n]*[А-Яа-яЁё][^"'`\n]{2,})["'`]""")
    for row in conn.execute("SELECT DISTINCT path FROM files WHERE path LIKE 'app/app/templates/%' OR path LIKE 'app/app/static/%.js'").fetchall():
        rel = row[0]
        seen: set[str] = set()
        for lineno, line in enumerate(_read(rel).splitlines(), 1):
            if rel.endswith(".html") and "<script" not in line:
                text = re.sub(r"\{[{%].*?[}%]\}", " ", re.sub(r"<[^>]+>", " ", line))
                candidates = [re.sub(r"\s+", " ", text).strip()] + [m.group(1) for m in re.finditer(r'(?:title|aria-label|placeholder)="([^"]*)"', line)]
            else:
                candidates = [m.group(1) for m in lit.finditer(line)]
            for c in candidates:
                c = c.strip()
                if len(c) >= 3 and cyr.search(c) and c not in seen and len(c) < 300:
                    seen.add(c)
                    conn.execute("INSERT INTO ui_text(text, path, line) VALUES (?,?,?)", (c, rel, lineno))
    chunks: list[Chunk] = []
    for eid, defs in ids.items():
        rows = conn.execute("SELECT * FROM ui_links WHERE element_id=?", (eid,)).fetchall()
        if not rows or not any(r["js_path"] for r in rows):
            continue
        r0 = rows[0]
        text = (f"Элемент #{eid} <{r0['tag']}> «{r0['label']}» — {r0['path']}:{r0['line']}\n" +
                "\n".join(f"  JS: {r['js_path']}:{r['js_line']} {r['js_symbol']}  API: {r['endpoints'] or '—'}" for r in rows[:6] if r["js_path"]))
        chunks.append(Chunk(r0["path"], "ui_link", eid, eid, r0["line"] or 1, r0["line"] or 1, text, {"generated": True}))
    return chunks


# ───────────────────────────── Тесты ─────────────────────────────

def _module_path(module: str) -> str | None:
    parts = module.lstrip(".").split(".")
    for cand in ("/".join(parts) + ".py", "/".join(parts) + "/__init__.py"):
        if (ROOT / cand).is_file():
            return cand
    return None


def analyze_tests(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM test_cover")
    def_paths: dict[str, list[str]] = {}
    for r in conn.execute("SELECT name, path FROM symbols WHERE kind NOT LIKE '%test%' AND kind!='module' AND path NOT LIKE '%/tests/%'").fetchall():
        def_paths.setdefault(r["name"], []).append(r["path"])
    tests = conn.execute("SELECT DISTINCT path FROM symbols WHERE path LIKE '%/tests/%' OR path LIKE 'tests/%' OR path LIKE '%/test\\_%' ESCAPE '\\'").fetchall()
    for t in tests:
        tp = t["path"]
        for imp in conn.execute("SELECT module, names, line FROM imports WHERE path=?", (tp,)).fetchall():
            candidates = [(_module_path(imp["module"]), imp["names"])]
            # `from app.app.services import requirement_review` — импортирован сам модуль
            for name in (imp["names"] or "").split(","):
                sub = _module_path(f"{imp['module']}.{name.strip()}") if name.strip() else None
                if sub:
                    candidates.append((sub, name.strip()))
            for mp, names in candidates:
                if mp and "/tests/" not in mp and not mp.endswith("__init__.py"):
                    conn.execute("INSERT INTO test_cover(test_path, test_name, line, module, symbol) VALUES (?,?,?,?,?)",
                                 (tp, "", imp["line"], mp, names))
        for fn in conn.execute("SELECT qualname, line FROM symbols WHERE path=? AND kind LIKE '%test%'", (tp,)).fetchall():
            callees = {c["callee"] for c in conn.execute("SELECT callee FROM calls WHERE path=? AND caller=?", (tp, fn["qualname"])).fetchall()}
            for name in callees:
                for mp in def_paths.get(name, [])[:3]:
                    conn.execute("INSERT INTO test_cover(test_path, test_name, line, module, symbol) VALUES (?,?,?,?,?)",
                                 (tp, fn["qualname"], fn["line"], mp, name))


def _norm_route(url: str) -> str:
    """`/projects/18/x?y=1`, `/projects/{pid}/x` и `/projects/{project_id}/x` — один шаблон."""
    url = url.split("?", 1)[0]
    url = re.sub(r"\$?\{[^}]*\}", "{}", url)
    return re.sub(r"/\d+(?=/|$)", "/{}", url).rstrip("/") or "/"


def analyze_test_routes(conn: sqlite3.Connection) -> None:
    """Тест ходит по URL роута (client.get("/projects/1/x")) — значит, покрывает обработчик и его модуль."""
    routes: dict[str, list[tuple[str, str]]] = {}
    for r in conn.execute("SELECT url, handler, path FROM routes"):
        routes.setdefault(_norm_route(r["url"]), []).append((r["handler"], r["path"]))
    tests = conn.execute("SELECT DISTINCT path FROM symbols WHERE path LIKE '%/tests/%' AND lang='py'").fetchall()
    for t in tests:
        tp = t["path"]
        try:
            tree = ast.parse(_read(tp))
        except (SyntaxError, OSError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seen: set[tuple[str, str]] = set()
            for sub in ast.walk(fn):
                url = _const_str(sub) if isinstance(sub, (ast.Constant, ast.JoinedStr)) else None
                if not url or not url.startswith("/") or len(url) < 3:
                    continue
                for handler, path in routes.get(_norm_route(url), []):
                    if (handler, path) in seen:
                        continue
                    seen.add((handler, path))
                    conn.execute("INSERT INTO test_cover(test_path, test_name, line, module, symbol) VALUES (?,?,?,?,?)",
                                 (tp, fn.name, fn.lineno, path, handler))


# ───────────────────────────── Инварианты ─────────────────────────────

def analyze_invariants(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM invariants")
    pm = ROOT / "docs/PROJECT-MAP.md"
    if pm.is_file():
        inside = False
        for lineno, line in enumerate(pm.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^##\s+13\.", line):
                inside = True
                continue
            if inside and re.match(r"^##\s+\d+\.", line):
                break
            if inside and line.startswith("- "):
                guards = ", ".join(dict.fromkeys(re.findall(r"`([\w./]*test_\w+(?:\.py)?)`", line)))
                conn.execute("INSERT INTO invariants(text, source, line, guard) VALUES (?,?,?,?)",
                             (line[2:].strip(), "docs/PROJECT-MAP.md", lineno, guards))
    dm = ROOT / "docs/DECISIONS.md"
    if dm.is_file():
        for lineno, line in enumerate(dm.read_text(encoding="utf-8").splitlines(), 1):
            if line.startswith("### "):
                conn.execute("INSERT INTO invariants(text, source, line, guard) VALUES (?,?,?,?)",
                             ("Решение: " + line[4:].strip(), "docs/DECISIONS.md", lineno, ""))


# ───────────────────────────── Служебные факты (ops) ─────────────────────────────

def analyze_ops() -> list[Chunk]:
    chunks: list[Chunk] = []
    env = ROOT / ".env.example"
    facts: list[str] = []
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if re.match(r"^\s*[A-Z][A-Z0-9_]*(PORT|HOST|URL|DIR)\s*=", line):
                facts.append(line.strip())
    logs = ROOT / "data" / "logs"
    log_names = sorted(p.name for p in logs.glob("*.log")) if logs.is_dir() else []
    data_dirs = sorted(p.name for p in (ROOT / "data").iterdir() if p.is_dir()) if (ROOT / "data").is_dir() else []
    scripts = []
    for p in sorted((ROOT / "scripts").glob("*.ps1")):
        first = next((l.strip("#<>. \t") for l in p.read_text(encoding="utf-8-sig", errors="replace").splitlines() if l.strip("#<>. \t") and not l.startswith("#requires")), "")
        scripts.append(f"{p.name}: {first[:100]}")
    text = ("Эксплуатация PrimaryAnalitics: порты, адреса, каталоги, логи, скрипты запуска.\n"
            "Настройки адресов и портов (.env.example):\n" + "\n".join(f"  {f}" for f in facts) +
            "\nЛоги (data/logs/): " + ", ".join(log_names) +
            "\nКаталоги данных (data/): " + ", ".join(data_dirs) +
            "\nСкрипты PowerShell (scripts/):\n" + "\n".join(f"  {s}" for s in scripts) +
            "\nПроцессы: API (uvicorn), GPU-воркер, Caddy (HTTPS), LiveKit, voice_cleaner, ASR-контейнеры (Docker), Ollama, LM Studio."
            "\nЖурнал API — data/logs/api.log, воркера — data/logs/worker.log.")
    chunks.append(Chunk("docs/OPERATIONS.md", "ops", "Эксплуатация: порты, логи, каталоги, скрипты", "ops", 1, 1, text, {"generated": True}))
    return chunks


# ───────────────────────────── История git ─────────────────────────────

def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def analyze_history(conn: sqlite3.Connection, limit: int = 500) -> list[Chunk]:
    conn.execute("DELETE FROM history")
    out = _git("log", f"-n{limit}", "--name-only", "--pretty=format:\x1e%H\x1f%aI\x1f%an\x1f%s")
    chunks: list[Chunk] = []
    for block in out.split("\x1e"):
        block = block.strip("\n")
        if not block:
            continue
        head, _, files = block.partition("\n")
        parts = head.split("\x1f")
        if len(parts) < 4:
            continue
        h, date, author, subject = parts[:4]
        flist = [f for f in files.splitlines() if f.strip()]
        conn.execute("INSERT OR REPLACE INTO history(hash, date, author, subject, files) VALUES (?,?,?,?,?)",
                     (h, date, author, subject, "\n".join(flist)))
        chunks.append(Chunk("git", "commit", f"{h[:7]} {subject[:80]}", h[:7], 1, 1,
                            f"Коммит {h[:7]} {date[:10]} {author}: {subject}\nфайлы: {', '.join(flist[:30])}", {"generated": True}))
    return chunks


def changed_symbols(conn: sqlite3.Connection) -> list[dict]:
    """Незакоммиченные правки, сопоставленные с функциями (по диапазонам строк изменённых блоков)."""
    status = _git("status", "--porcelain").splitlines()
    result: list[dict] = []
    for line in status:
        code, path = line[:2], line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ")[-1]
        entry: dict = {"path": path, "status": code.strip() or "?", "symbols": []}
        if code.strip() not in {"??", "D"} and path.endswith((".py", ".js", ".html", ".css")):
            diff = _git("diff", "-U0", "HEAD", "--", path)
            ranges = [(int(m.group(1)), int(m.group(1)) + int(m.group(2) or 1) - 1)
                      for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff, re.M)]
            syms = conn.execute("SELECT qualname, line, end_line FROM symbols WHERE path=? AND kind!='module'", (path,)).fetchall()
            hit = {s["qualname"] for s in syms for a, b in ranges if a <= s["end_line"] and b >= s["line"] and (s["end_line"] - s["line"]) < 400}
            entry["symbols"] = sorted(hit)[:12]
            entry["hunks"] = len(ranges)
        result.append(entry)
    return result


def analyze_all(conn: sqlite3.Connection, history_commits: int = 500) -> list[Chunk]:
    generated: list[Chunk] = []
    generated += analyze_routes(conn)
    generated += analyze_db(conn)
    generated += analyze_env(conn)
    generated += analyze_ui(conn)
    analyze_tests(conn)
    analyze_test_routes(conn)
    analyze_invariants(conn)
    generated += analyze_ops()
    generated += analyze_history(conn, history_commits)
    return generated
