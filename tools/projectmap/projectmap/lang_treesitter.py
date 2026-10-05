"""Общий разбор языков через tree-sitter: символы, вызовы, импорты, базовые классы по таблице правил языка.

Обход дерева один для всех языков; различия описаны в `Spec` (модуль `lang_specs`): какие типы узлов — классы и функции,
как достать имя, откуда брать базовые классы и как записан импорт. Для вызовов есть общее правило: имя берётся из поля `name` или
`function`, а всё, что слева от последней точки (`.`, `::`, `->`), считается квалификатором.
Если грамматики языка нет, файл остаётся на файловом уровне с пометкой в `error`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from projectmap import grammars
from projectmap.languages import Language
from projectmap.model import MODULE_CALLER, Call, FileFacts, Import, Symbol
from projectmap.paths import is_test_path

SEPARATORS = re.compile(r"^(?P<qualifier>.*?)(?:\?\.|\.|::|->|:)\s*(?P<name>[A-Za-z_$][\w$]*!?)\s*(?:<.*>)?\s*$", re.S)
IDENTIFIER_TYPES = frozenset({
    "identifier", "type_identifier", "simple_identifier", "scoped_type_identifier", "scoped_identifier", "qualified_identifier",
    "name", "qualified_name", "constant", "scope_resolution", "navigation_expression", "member_expression", "user_type",
    "generic_type", "attribute", "namespace_name", "nested_type_identifier", "nested_identifier", "dotted_identifier",
})
MAX_SIGNATURE = 140


@dataclass(frozen=True)
class Spec:
    """Правила языка: типы узлов и способы достать имена."""

    classes: dict[str, str] = field(default_factory=dict)  # тип узла -> вид символа (class, interface, enum, struct, trait, module)
    functions: dict[str, str] = field(default_factory=dict)  # тип узла -> вид (function, method)
    calls: frozenset[str] = frozenset()
    imports: frozenset[str] = frozenset()
    base_fields: tuple[str, ...] = ()  # поля класса, из которых берутся базовые классы
    base_types: frozenset[str] = frozenset()  # типы дочерних узлов класса, из которых берутся базовые классы
    import_patterns: tuple[re.Pattern[str], ...] = ()  # регулярки с группами `module`, необязательно `alias`
    name_of: Callable[..., str | None] | None = None  # особое правило имени: (узел, текст файла) -> имя
    call_of: Callable[..., tuple[str, str] | None] | None = None  # особое правило вызова: (узел, текст) -> (имя, квалификатор)
    kind_of: Callable[..., str] | None = None  # особый вид символа: (контекст, узел, вид по умолчанию) -> вид
    handle: Callable[..., bool] | None = None  # особый узел: (контекст, узел, область, в классе, вызывающий, обход) -> обработан ли
    skip_calls: frozenset[str] = frozenset()  # имена, которые вызовами проекта не считаем


class Context:
    """Состояние обхода одного файла."""

    def __init__(self, path: str, language: Language, spec: Spec, source: bytes) -> None:
        self.path = path
        self.language = language
        self.spec = spec
        self.source = source
        self.facts = FileFacts(path, language.id, language.tier)
        self.test_file = is_test_path(path)
        self.lines = source.decode("utf-8", "replace").splitlines()
        self.pending_bases: dict[str, list[str]] = {}  # имя типа -> базы, найденные не в объявлении типа (`impl Trait for Type`)

    def text(self, node) -> str:
        return self.source[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def line_of(self, node) -> int:
        return node.start_point[0] + 1

    def end_line_of(self, node) -> int:
        return node.end_point[0] + 1


def _field_text(ctx: Context, node, *names: str) -> str | None:
    for name in names:
        child = node.child_by_field_name(name)
        if child is not None:
            return ctx.text(child).strip()
    return None


def default_name(ctx: Context, node) -> str | None:
    name = _field_text(ctx, node, "name")
    if name:
        return re.split(r"[<(\s]", name, maxsplit=1)[0] or None
    return None


def split_target(text: str) -> tuple[str, str] | None:
    """`a.b.c(…)` -> (`c`, `a.b`); простое имя -> (имя, ``); выражения без имени (`f()()`, `x[0]()`) -> None."""
    text = text.strip()
    if not text or text[0] in "([{\"'":
        return None
    if re.fullmatch(r"[A-Za-z_$][\w$]*!?", text):
        return text, ""
    match = SEPARATORS.match(text)
    if not match:
        return None
    return match.group("name"), match.group("qualifier").strip()[:60]


def default_call(ctx: Context, node) -> tuple[str, str] | None:
    name_node = node.child_by_field_name("name") or node.child_by_field_name("method")
    qualifier_node = (node.child_by_field_name("object") or node.child_by_field_name("receiver")
                      or node.child_by_field_name("scope") or node.child_by_field_name("table"))
    if name_node is not None and qualifier_node is not None:
        return ctx.text(name_node).strip(), ctx.text(qualifier_node).strip()[:60]
    if name_node is not None and node.child_by_field_name("function") is None:
        return split_target(ctx.text(name_node))
    function = node.child_by_field_name("function")
    if function is None and node.child_count:
        function = node.children[0]
    return split_target(ctx.text(function)) if function is not None else None


def collect_bases(ctx: Context, node) -> list[str]:
    """Имена базовых классов и интерфейсов из полей и дочерних узлов, перечисленных в правилах языка."""
    spec = ctx.spec
    containers = [c for f in spec.base_fields if (c := node.child_by_field_name(f)) is not None]
    containers += [c for c in node.children if c.type in spec.base_types]
    bases: list[str] = []

    def gather(item) -> None:
        if item.type in IDENTIFIER_TYPES:
            text = re.sub(r"<.*>$", "", ctx.text(item).strip(), flags=re.S).strip()
            text = re.sub(r"\(.*\)$", "", text, flags=re.S).strip()
            if text and text not in bases:
                bases.append(text)
            return
        for child in item.children:
            if child.is_named:
                gather(child)

    for container in containers:
        gather(container)
    return bases


def _signature(ctx: Context, node) -> str:
    first = ctx.text(node).strip().splitlines()[0] if ctx.text(node).strip() else ""
    return first.strip()[:MAX_SIGNATURE]


def _module_tail(module: str) -> str:
    """Имя, под которым модуль виден по алиасу: `pkg/sub/file.ext` -> `file`, `a.b.C` -> `C`, `crate::a::b` -> `b`."""
    module = module.strip("\"'")
    if "::" not in module and any(sep in module for sep in "/\\:"):
        last = re.split(r"[/\\:]", module)[-1]
        return last.rsplit(".", 1)[0] if "." in last else last
    return re.split(r"[.:]+", module)[-1]


def _add_import(ctx: Context, module: str, node, names: list[str] | None = None, aliases: dict[str, str] | None = None) -> None:
    module = module.strip().strip("\"'`;")
    if module:
        ctx.facts.imports.append(Import(ctx.path, module, names or [], aliases or {}, ctx.line_of(node)))


def _imports_from_text(ctx: Context, node) -> None:
    text = ctx.text(node)
    for pattern in ctx.spec.import_patterns:
        for match in pattern.finditer(text):
            groups = match.groupdict()
            module = groups.get("module")
            if not module:
                continue
            alias = groups.get("alias")
            names: list[str] = []
            aliases: dict[str, str] = {}
            for item in (groups.get("names") or "").split(","):
                parts = re.split(r"\s+as\s+", item.strip())
                if parts[0]:
                    names.append(parts[0])
                    if len(parts) > 1:
                        aliases[parts[1]] = parts[0]
            if alias:
                aliases[alias] = _module_tail(module)
            _add_import(ctx, module, node, names, aliases)
        else:
            continue
        break


def _visit(ctx: Context, node, scope: list[str], in_class: bool, caller: str) -> None:
    spec = ctx.spec
    if spec.handle is not None and spec.handle(ctx, node, scope, in_class, caller, _visit):
        return
    node_type = node.type
    in_function = caller != MODULE_CALLER and not caller.endswith(".<class>")
    if node_type in spec.classes:
        name = spec.name_of(ctx, node) if spec.name_of else default_name(ctx, node)
        if name:
            qualname = ".".join(scope + [name])
            kind = spec.kind_of(ctx, node, spec.classes[node_type]) if spec.kind_of else spec.classes[node_type]
            ctx.facts.symbols.append(Symbol(
                name.rsplit(".", 1)[-1], qualname, kind, ctx.path, ctx.line_of(node), ctx.end_line_of(node),
                signature=_signature(ctx, node), bases=collect_bases(ctx, node), lang=ctx.language.id))
            for child in node.children:
                _visit(ctx, child, scope + name.split("."), True, f"{qualname}.<class>")
            return
    if node_type in spec.functions and not in_function:  # замыкание принадлежит объемлющей функции и своего символа не получает
        name = spec.name_of(ctx, node) if spec.name_of else default_name(ctx, node)
        if name:
            qualname = ".".join(scope + [name])
            kind = "test" if ctx.test_file else ("method" if in_class or "." in name else spec.functions[node_type])
            ctx.facts.symbols.append(Symbol(
                name.rsplit(".", 1)[-1], qualname, kind, ctx.path, ctx.line_of(node), ctx.end_line_of(node),
                signature=_signature(ctx, node), lang=ctx.language.id))
            for child in node.children:
                _visit(ctx, child, scope, False, qualname)
            return
    if node_type in spec.imports:
        _imports_from_text(ctx, node)
    elif node_type in spec.calls:
        target = spec.call_of(ctx, node) if spec.call_of else default_call(ctx, node)  # особое правило решает само, вызов ли это
        if target and target[0] not in spec.skip_calls:
            ctx.facts.calls.append(Call(ctx.path, caller, target[0], target[1], ctx.line_of(node)))
    for child in node.children:
        _visit(ctx, child, scope, in_class, caller)


def extract_treesitter(root: Path, path: str, text: str, language: Language, spec: Spec) -> FileFacts:
    """Факты одного файла не на Python. Нет грамматики — файловый уровень с пометкой."""
    source = text.encode("utf-8")
    facts = FileFacts(path, language.id, language.tier, lines=len(text.splitlines()))
    try:
        parser = grammars.parser_for(root, language.id)
    except grammars.GrammarUnavailable as exc:
        facts.error = str(exc)
        return facts
    tree = parser.parse(source)
    ctx = Context(path, language, spec, source)
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    ctx.facts.lines = facts.lines
    ctx.facts.symbols.append(Symbol(stem, path, "module", path, 1, max(facts.lines, 1), lang=language.id))
    _visit(ctx, tree.root_node, [], False, MODULE_CALLER)
    unique: dict[tuple[str, str], Symbol] = {}
    for symbol in ctx.facts.symbols:  # объявление и реализация (Objective-C, C++) дают один символ
        kept = unique.setdefault((symbol.qualname, symbol.kind), symbol)
        if kept is not symbol:
            kept.bases += [b for b in symbol.bases if b not in kept.bases]
    ctx.facts.symbols = list(unique.values())
    seen_imports: set[tuple[str, int]] = set()
    kept_imports = []
    for item in ctx.facts.imports:
        if (item.module, item.line) not in seen_imports:
            seen_imports.add((item.module, item.line))
            kept_imports.append(item)
    ctx.facts.imports = kept_imports
    for symbol in ctx.facts.symbols:
        for base in ctx.pending_bases.get(symbol.qualname, []):
            if base not in symbol.bases:
                symbol.bases.append(base)
    if tree.root_node.has_error:
        ctx.facts.error = "в коде есть участки, которые грамматика не разобрала"
    ctx.facts.calls.sort(key=lambda c: (c.line, c.caller, c.callee, c.qualifier))
    ctx.facts.imports.sort(key=lambda i: (i.line, i.module))
    return ctx.facts
