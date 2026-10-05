"""Адаптер SQLAlchemy: таблицы и столбцы моделей, кто читает и пишет таблицы.

Таблица — класс с `__tablename__`; столбцы — присваивания `Column(...)` / `mapped_column(...)`. Связи: класс модели `declares`
таблицу; функция `reads` / `writes` таблицу. Функция обращается к таблице, если в её теле (вне аннотаций) названа модель или в строке
SQL названа таблица; «пишет», если создаёт объект модели, вызывает `update`/`insert`/`delete` с моделью, сохраняет присвоенные поля через
`commit`/`flush` или выполняет SQL с INSERT/UPDATE/DELETE. Модели и тесты сами не считаются обращающимися.
"""

from __future__ import annotations

import ast
import re

from projectmap.adapters import Adapter, AdapterContext
from projectmap.graphbuf import symbol_id, table_id
from projectmap.paths import is_test_path

COLUMN_FACTORIES = {"Column", "mapped_column"}
SQL_WORDS = ("select ", "insert ", "update ", "delete ", "from ", "into ", "pragma")
SQL_WRITES = ("insert ", "update ", "delete ")


def _applies(ctx: AdapterContext) -> bool:
    return ctx.imports_stack("sqlalchemy")


def _const_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _models(ctx: AdapterContext) -> tuple[dict[str, str], dict[str, list[tuple[str, int, str]]]]:
    """Класс модели -> таблица и таблица -> столбцы (имя, строка, тип); регистрирует узлы таблиц."""
    class_table: dict[str, str] = {}
    columns: dict[str, list[tuple[str, int, str]]] = {}
    for path in ctx.paths(".py"):
        text = ctx.text(path)
        if not text or "__tablename__" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            table = None
            for stmt in cls.body:
                if isinstance(stmt, ast.Assign) and stmt.targets and getattr(stmt.targets[0], "id", "") == "__tablename__":
                    table = _const_str(stmt.value)
            if not table:
                continue
            class_table[cls.name] = table
            node_id = ctx.add_node(table_id(table), "table", table, path, cls.lineno, sub="таблица")
            symbol = ctx.resolver.by_path_name.get((path, cls.name))
            if symbol is not None:
                ctx.edges.add(symbol_id(path, cls.name), node_id, "declares", cls.lineno)
            for stmt in cls.body:
                target = value = annotation = None
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    target, value, annotation = stmt.target.id, stmt.value, ast.unparse(stmt.annotation)
                elif isinstance(stmt, ast.Assign) and stmt.targets and isinstance(stmt.targets[0], ast.Name):
                    target, value = stmt.targets[0].id, stmt.value
                if target and not target.startswith("__") and isinstance(value, ast.Call) \
                        and getattr(value.func, "id", getattr(value.func, "attr", "")) in COLUMN_FACTORIES:
                    kind = annotation or (ast.unparse(value.args[0]) if value.args else "")
                    columns.setdefault(table, []).append((target, stmt.lineno, kind[:60]))
    for table, items in columns.items():
        ctx.nodes[table_id(table)]["columns"] = ", ".join(f"{c}:{t}" if t else c for c, _, t in items)[:600]
    return class_table, columns


def _scopes(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    scopes: list[tuple[str, ast.AST]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append((node.name, node))
        elif isinstance(node, ast.ClassDef):
            scopes += [(f"{node.name}.{m.name}", m) for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return scopes


def _run(ctx: AdapterContext) -> None:
    class_table, columns = _models(ctx)
    tables = set(columns) | set(class_table.values())
    models = set(class_table)
    reads = writes = 0
    for path in ctx.paths(".py"):
        if "/models/" in f"/{path}" or is_test_path(path):
            continue
        text = ctx.text(path)
        if not text or not (models & set(re.findall(r"\b[A-Z]\w+\b", text)) or any(w in text.lower() for w in SQL_WORDS)):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for qualname, scope in _scopes(tree):
            symbol = ctx.resolver.by_path_name.get((path, qualname))
            if symbol is None:
                continue
            annotated: set[int] = set()
            for sub in ast.walk(scope):
                annotations = []
                if isinstance(sub, ast.arg) and sub.annotation is not None:
                    annotations.append(sub.annotation)
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.returns is not None:
                    annotations.append(sub.returns)
                if isinstance(sub, ast.AnnAssign):
                    annotations.append(sub.annotation)
                for annotation in annotations:
                    annotated.update(id(x) for x in ast.walk(annotation))
            names = {n.id for n in ast.walk(scope) if isinstance(n, ast.Name) and n.id in models and id(n) not in annotated}
            calls = [c for c in ast.walk(scope) if isinstance(c, ast.Call)]
            call_names = {getattr(c.func, "attr", getattr(c.func, "id", "")) for c in calls}
            persists = bool(call_names & {"commit", "flush"})
            attribute_store = any(isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) for n in ast.walk(scope))
            constructed = {getattr(c.func, "id", "") for c in calls} & models
            dml = {arg.id for c in calls if getattr(c.func, "id", getattr(c.func, "attr", "")) in {"update", "insert", "delete"}
                   for arg in c.args if isinstance(arg, ast.Name) and arg.id in models}
            used: dict[str, tuple[int, bool]] = {}
            for name in names:
                is_write = name in constructed or name in dml or (persists and attribute_store)
                line, previous = used.get(class_table[name], (scope.lineno, False))
                used[class_table[name]] = (line, previous or is_write)
            for const in ast.walk(scope):
                if isinstance(const, ast.Constant) and isinstance(const.value, str) and len(const.value) > 8:
                    low = const.value.lower()
                    if not any(k in low for k in SQL_WORDS):
                        continue
                    for table in tables:
                        if re.search(rf"\b{re.escape(table)}\b", low):
                            line, previous = used.get(table, (const.lineno, False))
                            used[table] = (const.lineno, previous or any(k in low for k in SQL_WRITES))
            for table, (line, is_write) in used.items():
                if table_id(table) in ctx.nodes:
                    ctx.edges.add(symbol_id(path, qualname), table_id(table), "writes" if is_write else "reads", line)
                    writes += is_write
                    reads += not is_write
    ctx.notes.append(f"таблиц БД: {len(tables)}; обращений: читают {reads}, пишут {writes}")


ADAPTER = Adapter("sqlalchemy", "Таблицы БД (SQLAlchemy)", _applies, _run)
