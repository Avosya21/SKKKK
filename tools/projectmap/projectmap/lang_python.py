"""Разбор Python стандартным `ast`: символы, вызовы, импорты, базовые классы.

Глубокий уровень: учитываются вложенные импорты (внутри функций), вызовы по принадлежности (метод класса, тело класса,
уровень модуля), функции, переданные как значение, константы модуля (`ВЕРХНИЙ_РЕГИСТР`). Синтаксическая ошибка не роняет разбор:
файл остаётся с символом-модулем и пометкой в `error`.
"""

from __future__ import annotations

import ast

from projectmap.model import MODULE_CALLER, REF, Call, FileFacts, Import, Symbol
from projectmap.paths import is_test_path

# встроенные функции: вызов `len(x)` связью проекта не является
BUILTIN_CALLS = frozenset({
    "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple", "isinstance", "print", "range", "sorted",
    "enumerate", "zip", "min", "max", "sum", "any", "all", "getattr", "setattr", "hasattr", "open", "type", "super",
    "repr", "iter", "next", "map", "filter", "reversed", "abs", "round", "bytes", "frozenset", "id", "format",
})
FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFINITION_NODES = FUNCTION_NODES + (ast.ClassDef,)


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0][:300] if (text or "").strip() else ""


def _base_name(node: ast.expr) -> str:
    """Имя базового класса без параметров типа: `Generic[T]` -> `Generic`, `mod.Base` остаётся `mod.Base`."""
    if isinstance(node, ast.Subscript):
        node = node.value
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return ""


def _calls_in(node: ast.AST, path: str, caller: str, known: set[str], out: list[Call]) -> None:
    """Вызовы в узле, включая вложенные функции и классы: замыкание принадлежит функции, определение внутри `if` на уровне
    модуля — модулю. Определения верхнего уровня и методы вызывающий обходит сам и сюда не передаёт."""
    stack: list[ast.AST] = [node]
    while stack:
        current = stack.pop()
        for child in ast.iter_child_nodes(current):
            stack.append(child)
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if isinstance(func, ast.Name):
                name, qualifier = func.id, ""
            elif isinstance(func, ast.Attribute):
                name = func.attr
                try:
                    qualifier = ast.unparse(func.value)[:40]
                except Exception:  # noqa: BLE001
                    qualifier = ""
            else:
                continue
            line = getattr(child, "lineno", 0)
            if name not in BUILTIN_CALLS:
                out.append(Call(path, caller, name, qualifier, line))
            for arg in list(child.args) + [kw.value for kw in child.keywords]:
                if isinstance(arg, ast.Name) and arg.id in known and arg.id != name:
                    out.append(Call(path, caller, arg.id, REF, line))


def _imports(tree: ast.AST, path: str) -> list[Import]:
    seen: set[tuple[str, tuple[str, ...]]] = set()
    out: list[Import] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                key = (alias.name, ())
                if key not in seen:
                    seen.add(key)
                    out.append(Import(path, alias.name, [], {alias.asname: alias.name} if alias.asname else {}, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            names = [a.name for a in node.names if a.name != "*"]
            key = (module, tuple(names))
            if key not in seen:
                seen.add(key)
                aliases = {a.asname: a.name for a in node.names if a.asname}
                out.append(Import(path, module, names, aliases, node.lineno))
    return sorted(out, key=lambda i: i.line)


def extract_python(path: str, text: str) -> FileFacts:
    """Факты одного Python-файла."""
    lines = text.splitlines()
    facts = FileFacts(path, "python", "deep", lines=len(lines))
    stem = path.rsplit("/", 1)[-1].removesuffix(".py").removesuffix(".pyi")
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        facts.symbols.append(Symbol(stem, path, "module", path, 1, max(len(lines), 1), lang="py"))
        facts.error = f"синтаксическая ошибка: {exc.__class__.__name__}"
        return facts
    test_file = is_test_path(path)
    facts.symbols.append(Symbol(stem, path, "module", path, 1, max(len(lines), 1), doc=_first_line(ast.get_docstring(tree)), lang="py"))
    facts.imports = _imports(tree, path)

    known: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, DEFINITION_NODES):
            known.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            known.update(a.asname or a.name for a in node.names)

    for node in tree.body:  # константы модуля: TOKEN_TTL_HOURS = 4
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        if target and target.isupper() and len(target) > 2 and getattr(node, "value", None) is not None:
            facts.symbols.append(Symbol(target, target, "constant", path, node.lineno, node.end_lineno or node.lineno,
                                        signature=lines[node.lineno - 1].strip()[:140], lang="py"))

    def add_function(node: ast.FunctionDef | ast.AsyncFunctionDef, qualname: str, in_class: bool) -> None:
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        kind = "test" if test_file else ("method" if in_class else "function")
        facts.symbols.append(Symbol(
            node.name, qualname, kind, path, start, node.end_lineno or node.lineno,
            signature=lines[node.lineno - 1].strip(), doc=_first_line(ast.get_docstring(node)),
            decorators="; ".join(ast.unparse(d)[:80] for d in node.decorator_list), lang="py"))
        _calls_in(node, path, qualname, known, facts.calls)

    def add_class(node: ast.ClassDef, qualname: str) -> None:
        bases = [b for b in (_base_name(base) for base in node.bases) if b]
        facts.symbols.append(Symbol(
            node.name, qualname, "class", path, node.lineno, node.end_lineno or node.lineno,
            signature=f"class {node.name}({', '.join(ast.unparse(b) for b in node.bases)})", doc=_first_line(ast.get_docstring(node)),
            decorators="; ".join(ast.unparse(d)[:80] for d in node.decorator_list), bases=bases, lang="py"))
        body_without_defs = ast.Module(body=[n for n in node.body if not isinstance(n, DEFINITION_NODES)], type_ignores=[])
        _calls_in(body_without_defs, path, f"{qualname}.<class>", known, facts.calls)
        for member in node.body:
            add_definition(member, qualname, True)

    def add_definition(node: ast.AST, prefix: str, in_class: bool) -> None:
        if isinstance(node, FUNCTION_NODES):
            qualname = node.name if not prefix else f"{prefix}.{node.name}"
            add_function(node, qualname, in_class)
        elif isinstance(node, ast.ClassDef):
            add_class(node, node.name if not prefix else f"{prefix}.{node.name}")

    for node in tree.body:
        add_definition(node, "", False)
    top_level = ast.Module(body=[n for n in tree.body if not isinstance(n, DEFINITION_NODES)], type_ignores=[])
    _calls_in(top_level, path, MODULE_CALLER, known, facts.calls)
    facts.calls.sort(key=lambda c: (c.line, c.caller, c.callee, c.qualifier))
    return facts
