"""Адаптер настроек: переменные окружения, которые код читает, и файлы-образцы, где они объявлены.

Читает: `os.environ.get("X")`, `os.getenv("X")`, `os.environ["X"]` (Python), поля классов `BaseSettings` (имя поля в верхнем регистре),
`process.env.X` и `import.meta.env.X` (JS/TS), `$env:X` (PowerShell). Объявляет: строки `NAME=значение` в `.env.example`, `.env.sample`
и `*.env`. Значения переменных в граф не попадают (там бывают секреты): только имена и признак «объявлена в образце».
"""

from __future__ import annotations

import ast
import re

from projectmap.adapters import Adapter, AdapterContext
from projectmap.graphbuf import env_id, file_id, symbol_id

JS_ENV = (re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)"), re.compile(r"process\.env\[\s*['\"]([A-Za-z_]\w*)['\"]\s*\]"),
          re.compile(r"import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)"))
POWERSHELL_ENV = re.compile(r"\$env:([A-Za-z_]\w*)", re.I)
ENV_LINE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]{2,})\s*=")
ENV_SAMPLES = (".env.example", ".env.sample", ".env.template")
GETTERS = {"os.getenv", "os.environ.get", "environ.get", "getenv"}


def _applies(ctx: AdapterContext) -> bool:
    return True


def _is_sample(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith(ENV_SAMPLES) or name.endswith(".env")


def _declare(ctx: AdapterContext, name: str, path: str, line: int, *, declared: bool = False) -> str:
    return ctx.add_node(env_id(name), "env", name, path, line, sub="окружение", **({"declared": True} if declared else {}))


def _python(ctx: AdapterContext, path: str, text: str) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    settings_classes = {s.qualname for s in ctx.facts[path].symbols if s.kind == "class" and any(b.endswith("BaseSettings") for b in s.bases)}
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Call) and node.args and ast.unparse(node.func) in GETTERS and isinstance(node.args[0], ast.Constant):
            name = node.args[0].value
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value) in ("os.environ", "environ") and isinstance(node.slice, ast.Constant):
            name = node.slice.value
        if isinstance(name, str) and name:
            node_id = _declare(ctx, name, path, node.lineno)
            ctx.edges.add(ctx.owner_of(path, node.lineno), node_id, "reads_env", node.lineno)
        if isinstance(node, ast.ClassDef) and node.name in settings_classes:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and not stmt.target.id.startswith("_"):
                    field_node = _declare(ctx, stmt.target.id.upper(), path, stmt.lineno)
                    ctx.edges.add(symbol_id(path, node.name), field_node, "reads_env", stmt.lineno)


def _run(ctx: AdapterContext) -> None:
    for path in ctx.paths():
        text = ctx.text(path)
        if not text:
            continue
        if _is_sample(path):
            for number, line in enumerate(text.splitlines(), 1):
                match = ENV_LINE.match(line)
                if match:
                    ctx.edges.add(file_id(path), _declare(ctx, match.group(1), path, number, declared=True), "declares", number)
        elif path.endswith(".py"):
            _python(ctx, path, text)
        elif path.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".html")):
            for number, line in enumerate(text.splitlines(), 1):
                for pattern in JS_ENV:
                    for match in pattern.finditer(line):
                        ctx.edges.add(ctx.owner_of(path, number), _declare(ctx, match.group(1), path, number), "reads_env", number)
        elif path.endswith((".ps1", ".psm1")):
            for number, line in enumerate(text.splitlines(), 1):
                for match in POWERSHELL_ENV.finditer(line):
                    ctx.edges.add(ctx.owner_of(path, number), _declare(ctx, match.group(1), path, number), "reads_env", number)
    ctx.notes.append(f"переменных окружения: {sum(1 for n in ctx.nodes.values() if n['kind'] == 'env')}")


ADAPTER = Adapter("env", "Переменные окружения", _applies, _run)
