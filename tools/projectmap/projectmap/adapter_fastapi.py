"""Адаптер FastAPI (и совместимых: Starlette, APIRouter): веб-роуты, обработчики, зависимости и проверки прав.

Узел `route` на каждый метод и адрес с префиксом роутера (`APIRouter(prefix=…)` и `include_router(…, prefix=…)`). Связи: `handles`
роут -> функция-обработчик, `depends_on` роут -> функции из `Depends(...)`, `guarded_by` роут -> функции проверки прав
(`require_*`, `ensure_*`, `check_*`, `can_*`, `verify_*`, `assert_*`) и сравнение роли пользователя в теле обработчика.
"""

from __future__ import annotations

import ast
import re

from projectmap.adapters import Adapter, AdapterContext
from projectmap.graphbuf import file_id, route_id, symbol_id
from projectmap.model import Call

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "websocket"}
GUARD_CALL = re.compile(r"^_?(require|ensure|check|can|assert|verify)\w*", re.I)


def _const_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _applies(ctx: AdapterContext) -> bool:
    return ctx.imports_stack("fastapi", "starlette")


def _module_of(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".py")


def _prefixes(ctx: AdapterContext) -> dict[str, str]:
    """Имя модуля роутера -> префикс из `include_router(module.router, prefix=…)` в любом файле."""
    prefixes: dict[str, str] = {}
    for path in ctx.paths(".py"):
        text = ctx.text(path)
        if not text or "include_router" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "include_router" and node.args:
                arg = node.args[0]
                prefix = next((_const_str(k.value) for k in node.keywords if k.arg == "prefix"), "") or ""
                if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                    prefixes[arg.value.id] = prefix
    return prefixes


def _run(ctx: AdapterContext) -> None:
    prefixes = _prefixes(ctx)
    count = 0
    for path in ctx.paths(".py"):
        text = ctx.text(path)
        if not text or not any(h in text for h in (".get(", ".post(", ".put(", ".delete(", ".patch(", ".websocket(")):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        router_prefix: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and node.targets and isinstance(node.targets[0], ast.Name):
                fn = node.value.func
                if getattr(fn, "id", getattr(fn, "attr", "")) == "APIRouter":
                    router_prefix[node.targets[0].id] = next((_const_str(k.value) for k in node.value.keywords if k.arg == "prefix"), "") or ""
        extra = prefixes.get(_module_of(path), "")
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in func.decorator_list:
                if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and deco.func.attr in HTTP_METHODS):
                    continue
                tail = _const_str(deco.args[0]) if deco.args else ""
                if tail is None:
                    continue
                owner = deco.func.value.id if isinstance(deco.func.value, ast.Name) else ""
                url = f"{extra}{router_prefix.get(owner, '')}{tail}" or "/"
                method = deco.func.attr.upper()
                deps: list[str] = []
                for sub in ast.walk(func.args):
                    if isinstance(sub, ast.Call) and getattr(sub.func, "id", "") == "Depends" and sub.args:
                        deps.append(ast.unparse(sub.args[0]))
                guards = [d for d in deps if GUARD_CALL.match(d.split("(")[0])]
                for sub in ast.walk(func):
                    if isinstance(sub, ast.Call):
                        name = getattr(sub.func, "id", getattr(sub.func, "attr", ""))
                        if GUARD_CALL.match(name):
                            guards.append(name)
                    elif isinstance(sub, ast.Compare) and "role" in ast.unparse(sub.left):
                        guards.append(ast.unparse(sub)[:60])
                guards = list(dict.fromkeys(guards))
                node_id = ctx.add_node(route_id(method, url), "route", f"{method} {url}", path, func.lineno, sub=method, handler=func.name,
                                       guards=", ".join(guards), deps=", ".join(dict.fromkeys(deps)),
                                       doc=(ast.get_docstring(func) or "")[:160])
                count += 1
                owner_symbol = ctx.resolver.by_path_name.get((path, func.name))
                if owner_symbol is not None:
                    ctx.edges.add(node_id, symbol_id(path, owner_symbol.qualname), "handles", func.lineno)
                for dep in dict.fromkeys(deps):
                    found = ctx.resolver.resolve_call(Call(path, "<module>", dep.split("(")[0].rsplit(".", 1)[-1], "", func.lineno))
                    if found.symbol is not None:
                        ctx.edges.add(node_id, symbol_id(found.symbol.path, found.symbol.qualname), "depends_on", func.lineno)
                for guard in guards:
                    name = guard.split("(")[0]
                    if GUARD_CALL.match(name):
                        found = ctx.resolver.resolve_call(Call(path, "<module>", name.rsplit(".", 1)[-1], "", func.lineno))
                        if found.symbol is not None:
                            ctx.edges.add(node_id, symbol_id(found.symbol.path, found.symbol.qualname), "guarded_by", func.lineno)
                ctx.edges.add(file_id(path), node_id, "declares", func.lineno)
    ctx.notes.append(f"роутов: {count}")


ADAPTER = Adapter("fastapi", "Веб-роуты FastAPI", _applies, _run)
