"""Правила языков для общего разбора tree-sitter (`lang_treesitter`): типы узлов, имена, базовые классы, импорты.

Каждое правило проверено на образце кода в тестах `test_lang_treesitter.py`. Что язык умеет, записано в `CAPABILITIES`:
символы, вызовы, импорты, базовые классы; отсутствующее в списке этого языка разбор не даёт (и не выдумывает).
"""

from __future__ import annotations

import re

from projectmap.lang_treesitter import Context, Spec, _add_import, default_call, default_name, split_target
from projectmap.languages import Language
from projectmap.model import Symbol

JS_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function", "generator_function"})
GO_BUILTINS = frozenset({"make", "new", "len", "cap", "append", "copy", "delete", "panic", "recover", "print", "println", "close"})


def _unquote(text: str) -> str:
    return text.strip().strip("\"'`")


# ───────────────────────────── JavaScript / TypeScript ─────────────────────────────

def _js_handle(ctx: Context, node, scope, in_class, caller, visit) -> bool:
    """Особые узлы JS/TS: стрелочные функции в переменных, импорты, `require`, `export ... from`."""
    kind = node.type
    in_function = caller != "<module>" and not caller.endswith(".<class>")
    if kind == "variable_declarator" and not in_function:
        value = node.child_by_field_name("value")
        name_node = node.child_by_field_name("name")
        if value is not None and name_node is not None and value.type in JS_FUNCTION_VALUES and name_node.type == "identifier":
            name = ctx.text(name_node)
            qualname = ".".join(scope + [name])
            ctx.facts.symbols.append(Symbol(name, qualname, "test" if ctx.test_file else "function", ctx.path, ctx.line_of(node),
                                            ctx.end_line_of(node), signature=ctx.text(node).strip().splitlines()[0][:140],
                                            lang=ctx.language.id))
            for child in value.children:
                visit(ctx, child, scope, False, qualname)
            return True
        return False
    if kind in ("import_statement", "export_statement"):
        source = node.child_by_field_name("source")
        if source is not None:
            names: list[str] = []
            aliases: dict[str, str] = {}

            def gather(item) -> None:
                if item.type in ("import_specifier", "export_specifier"):
                    original = item.child_by_field_name("name")
                    alias = item.child_by_field_name("alias")
                    if original is not None:
                        names.append(ctx.text(original))
                        if alias is not None:
                            aliases[ctx.text(alias)] = ctx.text(original)
                    return
                if item.type == "identifier" and item.parent is not None and item.parent.type == "import_clause":
                    names.append("default")
                    aliases[ctx.text(item)] = "default"
                    return
                for child in item.children:
                    gather(child)

            gather(node)
            _add_import(ctx, ctx.text(source), node, names, aliases)
            return True
        return False
    if kind == "call_expression":
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is not None and arguments is not None and ctx.text(function) in ("require", "import") and arguments.named_child_count:
            first = arguments.named_children[0]
            if first.type == "string":
                _add_import(ctx, ctx.text(first), node)
                return True
    return False


def _js_call(ctx: Context, node) -> tuple[str, str] | None:
    if node.type == "new_expression":
        constructor = node.child_by_field_name("constructor")
        return split_target(ctx.text(constructor)) if constructor is not None else None
    return default_call(ctx, node)


JS = Spec(
    classes={"class_declaration": "class", "abstract_class_declaration": "class", "interface_declaration": "interface",
             "enum_declaration": "enum", "class": "class"},
    functions={"function_declaration": "function", "generator_function_declaration": "function", "method_definition": "method",
               "abstract_method_signature": "method"},
    calls=frozenset({"call_expression", "new_expression"}),
    base_types=frozenset({"class_heritage", "extends_type_clause", "extends_clause", "implements_clause"}),
    handle=_js_handle,
    call_of=_js_call,
    skip_calls=frozenset({"super", "require", "import"}),
)


# ───────────────────────────── Java ─────────────────────────────

JAVA_IMPORT = re.compile(r"import\s+(?:static\s+)?(?P<module>[\w.]+(?:\.\*)?)\s*;")


def _java_call(ctx: Context, node) -> tuple[str, str] | None:
    if node.type == "object_creation_expression":
        created = node.child_by_field_name("type")
        if created is None:
            return None
        found = split_target(re.sub(r"<.*>$", "", ctx.text(created).strip(), flags=re.S))
        return found
    return default_call(ctx, node)


JAVA = Spec(
    classes={"class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum",
             "record_declaration": "class", "annotation_type_declaration": "interface"},
    functions={"method_declaration": "method", "constructor_declaration": "method"},
    calls=frozenset({"method_invocation", "object_creation_expression"}),
    imports=frozenset({"import_declaration"}),
    base_fields=("superclass", "interfaces"),
    base_types=frozenset({"extends_interfaces"}),
    import_patterns=(JAVA_IMPORT,),
    call_of=_java_call,
    skip_calls=frozenset({"super", "this"}),
)


# ───────────────────────────── Go ─────────────────────────────

def _go_receiver_type(ctx: Context, node) -> str | None:
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return None
    match = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*\)\s*$", ctx.text(receiver))
    return match.group(1) if match else None


def _go_handle(ctx: Context, node, scope, in_class, caller, visit) -> bool:
    if node.type == "method_declaration":
        name = default_name(ctx, node)
        owner = _go_receiver_type(ctx, node)
        if name:
            qualname = f"{owner}.{name}" if owner else name
            ctx.facts.symbols.append(Symbol(name, qualname, "test" if ctx.test_file else "method", ctx.path, ctx.line_of(node),
                                            ctx.end_line_of(node), signature=ctx.text(node).strip().splitlines()[0][:140],
                                            lang=ctx.language.id))
            for child in node.children:
                visit(ctx, child, scope, False, qualname)
            return True
    if node.type == "type_spec":
        name = default_name(ctx, node)
        body = node.child_by_field_name("type")
        if name and body is not None:
            kind = {"struct_type": "struct", "interface_type": "interface"}.get(body.type, "type")
            ctx.facts.symbols.append(Symbol(name, ".".join(scope + [name]), kind, ctx.path, ctx.line_of(node), ctx.end_line_of(node),
                                            signature=ctx.text(node).strip().splitlines()[0][:140], lang=ctx.language.id))
            return True
    if node.type == "import_spec":
        path = node.child_by_field_name("path")
        alias = node.child_by_field_name("name")
        if path is not None:
            module = _unquote(ctx.text(path))
            last = module.rsplit("/", 1)[-1]
            _add_import(ctx, module, node, None, {ctx.text(alias): last} if alias is not None and ctx.text(alias) not in ("_", ".") else None)
            return True
    return False


GO = Spec(
    functions={"function_declaration": "function"},
    calls=frozenset({"call_expression"}),
    handle=_go_handle,
    skip_calls=GO_BUILTINS,
)


# ───────────────────────────── вспомогательные правила ─────────────────────────────

def _drop_generics(text: str) -> str:
    return re.sub(r"<.*>$", "", text.strip(), flags=re.S).strip()


def _field_text_of(ctx: Context, node, field: str) -> str | None:
    child = node.child_by_field_name(field)
    return ctx.text(child) if child is not None else None


def _string_argument(ctx: Context, node) -> str | None:
    """Первый строковый аргумент вызова (`require 'x'`, `source x`); без кавычек."""
    for child in node.named_children:
        target = child
        while target.type in ("argument_list", "arguments", "argument") and target.named_child_count:
            target = target.named_children[0]
        if target.type in ("string", "string_literal", "raw_string", "word", "string_content", "encapsed_string"):
            return _unquote(ctx.text(target))
    return None


# ───────────────────────────── Rust ─────────────────────────────

RUST_USE = re.compile(r"use\s+(?P<module>[\w:]+?)(?:::\{(?P<names>[^}]*)\})?(?:\s+as\s+(?P<alias>\w+))?\s*;")


def _rust_handle(ctx: Context, node, scope, in_class, caller, visit) -> bool:
    if node.type != "impl_item":
        return False
    type_node = node.child_by_field_name("type")
    body = node.child_by_field_name("body")
    if type_node is None or body is None:
        return False
    owner = _drop_generics(ctx.text(type_node)).lstrip("&").strip().replace("::", ".")
    trait_node = node.child_by_field_name("trait")
    if trait_node is not None:
        ctx.pending_bases.setdefault(owner, []).append(_drop_generics(ctx.text(trait_node)).replace("::", "."))
    for child in body.children:
        visit(ctx, child, scope + [owner], True, ".".join(scope + [owner]) + ".<class>")
    return True


RUST = Spec(
    classes={"struct_item": "struct", "trait_item": "trait", "enum_item": "enum", "union_item": "struct", "mod_item": "module"},
    functions={"function_item": "function", "function_signature_item": "method"},
    calls=frozenset({"call_expression"}),
    imports=frozenset({"use_declaration"}),
    import_patterns=(RUST_USE,),
    handle=_rust_handle,
)


# ───────────────────────────── C# ─────────────────────────────

CSHARP_USING = re.compile(r"using\s+(?:static\s+)?(?:(?P<alias>\w+)\s*=\s*)?(?P<module>[\w.]+)\s*;")


def _csharp_call(ctx: Context, node) -> tuple[str, str] | None:
    if node.type == "object_creation_expression":
        created = node.child_by_field_name("type")
        return split_target(_drop_generics(ctx.text(created))) if created is not None else None
    return default_call(ctx, node)


CSHARP = Spec(
    classes={"class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum",
             "struct_declaration": "struct", "record_declaration": "class"},
    functions={"method_declaration": "method", "constructor_declaration": "method"},
    calls=frozenset({"invocation_expression", "object_creation_expression"}),
    imports=frozenset({"using_directive"}),
    base_types=frozenset({"base_list"}),
    import_patterns=(CSHARP_USING,),
    call_of=_csharp_call,
    skip_calls=frozenset({"base", "this", "nameof", "typeof"}),
)


# ───────────────────────────── C и C++ ─────────────────────────────

C_INCLUDE = re.compile(r"#\s*(?:include|import)\s*[<\"](?P<module>[^>\"]+)[>\"]")
C_NAME_TYPES = frozenset({"identifier", "field_identifier", "qualified_identifier", "destructor_name", "operator_name"})


def _c_function_name(ctx: Context, node) -> str | None:
    declarator = node.child_by_field_name("declarator")
    while declarator is not None and declarator.type not in C_NAME_TYPES:
        declarator = declarator.child_by_field_name("declarator")
    return ctx.text(declarator).replace("::", ".").strip() if declarator is not None else None


def _c_class_name(ctx: Context, node) -> str | None:
    if node.child_by_field_name("body") is None:
        return None  # объявление без тела (`struct S;`) символом не считаем
    return default_name(ctx, node)


def _c_name(ctx: Context, node) -> str | None:
    return _c_function_name(ctx, node) if node.type == "function_definition" else _c_class_name(ctx, node)


C_NOISE = frozenset({"sizeof", "malloc", "free", "printf", "fprintf", "sprintf", "snprintf", "memcpy", "memset", "strlen", "strcmp"})
C = Spec(
    classes={"struct_specifier": "struct", "enum_specifier": "enum", "union_specifier": "struct"},
    functions={"function_definition": "function"},
    calls=frozenset({"call_expression"}),
    imports=frozenset({"preproc_include"}),
    import_patterns=(C_INCLUDE,),
    name_of=_c_name,
    skip_calls=C_NOISE,
)
CPP = Spec(
    classes={"class_specifier": "class", "struct_specifier": "struct", "enum_specifier": "enum", "union_specifier": "struct"},
    functions={"function_definition": "function"},
    calls=frozenset({"call_expression"}),
    imports=frozenset({"preproc_include"}),
    base_types=frozenset({"base_class_clause"}),
    import_patterns=(C_INCLUDE,),
    name_of=_c_name,
    skip_calls=C_NOISE | frozenset({"static_cast", "dynamic_cast", "reinterpret_cast", "const_cast"}),
)


# ───────────────────────────── Ruby ─────────────────────────────

RUBY_NOISE = frozenset({"puts", "print", "p", "require", "require_relative", "load", "raise", "attr_reader", "attr_writer", "attr_accessor",
                        "include", "extend", "private", "public", "protected", "lambda", "proc", "new"})


def _ruby_handle(ctx: Context, node, scope, in_class, caller, visit) -> bool:
    if node.type == "call":
        method = node.child_by_field_name("method")
        if method is not None and ctx.text(method) in ("require", "require_relative", "load") and node.child_by_field_name("receiver") is None:
            module = _string_argument(ctx, node)
            if module:
                relative = ctx.text(method) != "require" and not module.startswith(".")
                _add_import(ctx, ("./" + module) if relative else module, node)
                return True
    return False


def _ruby_name(ctx: Context, node) -> str | None:
    name = _field_text_of(ctx, node, "name")
    return name.replace("::", ".").strip() if name else None


RUBY = Spec(
    classes={"class": "class", "module": "module"},
    functions={"method": "function", "singleton_method": "method"},
    calls=frozenset({"call"}),
    base_fields=("superclass",),
    handle=_ruby_handle,
    name_of=_ruby_name,
    skip_calls=RUBY_NOISE,
)


# ───────────────────────────── PHP ─────────────────────────────

PHP_USE = re.compile(r"use\s+(?:function\s+|const\s+)?(?P<module>\\?[\w\\]+)(?:\s+as\s+(?P<alias>\w+))?\s*;")


def _php_call(ctx: Context, node) -> tuple[str, str] | None:
    if node.type == "object_creation_expression":
        for child in node.named_children:
            if child.type in ("name", "qualified_name"):
                return split_target(ctx.text(child).replace("\\", "."))
        return None
    return default_call(ctx, node)


def _php_handle(ctx: Context, node, scope, in_class, caller, visit) -> bool:
    if node.type in ("require_expression", "require_once_expression", "include_expression", "include_once_expression"):
        module = _string_argument(ctx, node)
        if module:
            _add_import(ctx, module, node)
            return True
    return False


PHP = Spec(
    classes={"class_declaration": "class", "interface_declaration": "interface", "trait_declaration": "trait", "enum_declaration": "enum"},
    functions={"function_definition": "function", "method_declaration": "method"},
    calls=frozenset({"function_call_expression", "member_call_expression", "scoped_call_expression", "object_creation_expression"}),
    imports=frozenset({"namespace_use_declaration"}),
    base_types=frozenset({"base_clause", "class_interface_clause"}),
    import_patterns=(PHP_USE,),
    handle=_php_handle,
    call_of=_php_call,
    skip_calls=frozenset({"isset", "empty", "unset", "die", "exit", "array", "echo", "print"}),
)


# ───────────────────────────── Swift ─────────────────────────────

SWIFT_IMPORT = re.compile(r"import\s+(?:\w+\s+)?(?P<module>[\w.]+)")
SWIFT_KINDS = {"struct": "struct", "enum": "enum", "protocol": "interface"}


def _swift_name(ctx: Context, node) -> str | None:
    if node.type == "init_declaration":
        return "init"
    return default_name(ctx, node)


def _swift_kind(ctx: Context, node, default: str) -> str:
    return SWIFT_KINDS.get((_field_text_of(ctx, node, "declaration_kind") or "").strip(), default)


SWIFT = Spec(
    classes={"class_declaration": "class", "protocol_declaration": "interface"},
    functions={"function_declaration": "function", "protocol_function_declaration": "method", "init_declaration": "method"},
    calls=frozenset({"call_expression"}),
    imports=frozenset({"import_declaration"}),
    base_types=frozenset({"inheritance_specifier"}),
    import_patterns=(SWIFT_IMPORT,),
    name_of=_swift_name,
    kind_of=_swift_kind,
)


# ───────────────────────────── Scala ─────────────────────────────

SCALA_IMPORT = re.compile(r"import\s+(?P<module>[\w.]+?)(?:\.\{(?P<names>[^}]*)\})?\s*$", re.M)

SCALA = Spec(
    classes={"class_definition": "class", "object_definition": "class", "trait_definition": "trait"},
    functions={"function_definition": "function", "function_declaration": "function"},
    calls=frozenset({"call_expression"}),
    imports=frozenset({"import_declaration"}),
    base_fields=("extend",),
    import_patterns=(SCALA_IMPORT,),
)


# ───────────────────────────── Lua ─────────────────────────────

LUA_NOISE = frozenset({"require", "print", "pairs", "ipairs", "type", "tostring", "tonumber", "setmetatable", "getmetatable", "pcall", "error",
                       "assert", "select", "next", "rawget", "rawset", "unpack"})


def _lua_handle(ctx: Context, node, scope, in_class, caller, visit) -> bool:
    if node.type == "function_call":
        name = node.child_by_field_name("name")
        if name is not None and ctx.text(name) == "require":
            module = _string_argument(ctx, node.child_by_field_name("arguments") or node)
            if module:
                _add_import(ctx, module, node)
                return True
    return False


def _lua_name(ctx: Context, node) -> str | None:
    name = _field_text_of(ctx, node, "name")
    return name.replace(":", ".").strip() if name else None


LUA = Spec(
    functions={"function_declaration": "function"},
    calls=frozenset({"function_call"}),
    handle=_lua_handle,
    name_of=_lua_name,
    skip_calls=LUA_NOISE,
)


# ───────────────────────────── Shell ─────────────────────────────

BASH_BUILTINS = frozenset({
    "echo", "cd", "ls", "cp", "mv", "rm", "mkdir", "cat", "grep", "sed", "awk", "exit", "return", "export", "set", "unset", "test", "[", "[[",
    "read", "printf", "local", "declare", "shift", "trap", "eval", "exec", "true", "false", "source", ".", "pwd", "chmod", "touch", "sleep", "wait"})


def _bash_call(ctx: Context, node) -> tuple[str, str] | None:
    name = node.child_by_field_name("name")
    if name is None:
        return None
    text = ctx.text(name).strip().strip("\"'")
    if not text or text.startswith(("$", "-")):
        return None
    return text.rsplit("/", 1)[-1], ""


def _bash_handle(ctx: Context, node, scope, in_class, caller, visit) -> bool:
    if node.type == "command":
        name = node.child_by_field_name("name")
        if name is not None and ctx.text(name).strip() in ("source", "."):
            arguments = [c for c in node.named_children if c.type != "command_name"]
            if arguments:
                _add_import(ctx, ctx.text(arguments[0]), node)
                return True
    return False


BASH = Spec(
    functions={"function_definition": "function"},
    calls=frozenset({"command"}),
    handle=_bash_handle,
    call_of=_bash_call,
    skip_calls=BASH_BUILTINS,
)


# ───────────────────────────── Kotlin ─────────────────────────────

def _first_child_text(ctx: Context, node, types: tuple[str, ...]) -> str | None:
    for child in node.children:
        if child.type in types:
            return ctx.text(child).strip()
    return None


def _kotlin_name(ctx: Context, node) -> str | None:
    return _first_child_text(ctx, node, ("type_identifier", "simple_identifier"))


def _kotlin_kind(ctx: Context, node, default: str) -> str:
    head = ctx.text(node).split("{", 1)[0]
    if re.search(r"\binterface\b", head):
        return "interface"
    if re.search(r"\benum\b", head):
        return "enum"
    return default


def _kotlin_handle(ctx: Context, node, scope, in_class, caller, visit) -> bool:
    if node.type != "import_header":
        return False
    identifier = next((c for c in node.children if c.type == "identifier"), None)
    alias = next((c for c in node.children if c.type == "import_alias"), None)
    if identifier is None:
        return False
    module = ctx.text(identifier).strip()
    aliases = {_first_child_text(ctx, alias, ("type_identifier", "simple_identifier")) or "": module.rsplit(".", 1)[-1]} if alias is not None else {}
    _add_import(ctx, module, node, None, {k: v for k, v in aliases.items() if k})
    return True


KOTLIN = Spec(
    classes={"class_declaration": "class", "object_declaration": "class"},
    functions={"function_declaration": "function"},
    calls=frozenset({"call_expression"}),
    base_types=frozenset({"delegation_specifier"}),
    handle=_kotlin_handle,
    name_of=_kotlin_name,
    kind_of=_kotlin_kind,
    skip_calls=frozenset({"println", "print", "listOf", "mutableListOf", "mapOf", "setOf", "arrayOf", "require", "check", "error", "lazy"}),
)


# ───────────────────────────── PowerShell ─────────────────────────────

POWERSHELL_NOISE = frozenset({
    "using", "write-host", "write-output", "write-error", "write-warning", "write-verbose", "write-debug", "get-item", "get-childitem", "test-path",
    "join-path", "split-path", "set-location", "out-null", "foreach-object", "where-object", "select-object", "get-content", "set-content",
    "start-process", "new-item", "remove-item", "copy-item", "move-item", "get-date", "get-command", "out-file", "convertto-json",
    "convertfrom-json", "invoke-webrequest", "invoke-restmethod", "start-sleep", "exit", "return", "throw"})


def _powershell_name(ctx: Context, node) -> str | None:
    return _first_child_text(ctx, node, ("function_name",))


def _powershell_command_name(ctx: Context, node) -> str | None:
    name = node.child_by_field_name("command_name")
    if name is None:
        return None
    inner = next((c for c in name.children if c.type == "command_name"), None)
    return ctx.text(inner if inner is not None else name).strip()


def _powershell_handle(ctx: Context, node, scope, in_class, caller, visit) -> bool:
    if node.type != "command":
        return False
    name = _powershell_command_name(ctx, node)
    if name is None:
        return False
    operator = next((c for c in node.children if c.type == "command_invokation_operator"), None)
    if operator is not None and ctx.text(operator).strip() == ".":  # `. ./lib.ps1` — подключение скрипта
        _add_import(ctx, name, node)
        return True
    if name.lower() == "import-module":
        elements = node.child_by_field_name("command_elements")
        words = [c for c in (elements.named_children if elements is not None else []) if c.type in ("generic_token", "string", "expandable_string_literal")]
        if words:
            _add_import(ctx, ctx.text(words[0]), node)
            return True
    return False


def _powershell_call(ctx: Context, node) -> tuple[str, str] | None:
    name = _powershell_command_name(ctx, node)
    if not name or name.startswith(("$", ".", "&")) or name.lower() in POWERSHELL_NOISE:
        return None
    return name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1], ""


POWERSHELL = Spec(
    functions={"function_statement": "function"},
    calls=frozenset({"command"}),
    handle=_powershell_handle,
    name_of=_powershell_name,
    call_of=_powershell_call,
)


# ───────────────────────────── Objective-C ─────────────────────────────

def _objc_name(ctx: Context, node) -> str | None:
    return _first_child_text(ctx, node, ("identifier",))


OBJC = Spec(
    classes={"class_interface": "class", "class_implementation": "class"},
    functions={"method_definition": "method", "function_definition": "function"},
    calls=frozenset({"message_expression", "call_expression"}),
    imports=frozenset({"preproc_include", "preproc_import"}),
    base_fields=("superclass",),
    import_patterns=(C_INCLUDE,),
    name_of=lambda ctx, node: _c_function_name(ctx, node) if node.type == "function_definition" else _objc_name(ctx, node),
)


# ───────────────────────────── Dart ─────────────────────────────

DART_IMPORT = re.compile(r"(?:import|export)\s+['\"](?P<module>[^'\"]+)['\"](?:\s+as\s+(?P<alias>\w+))?")

DART = Spec(
    classes={"class_definition": "class", "mixin_declaration": "class", "enum_declaration": "enum", "extension_declaration": "class"},
    functions={"function_signature": "function"},
    imports=frozenset({"import_or_export", "library_import"}),
    base_fields=("superclass", "interfaces"),
    import_patterns=(DART_IMPORT,),
)


SPECS: dict[str, Spec] = {
    "javascript": JS, "typescript": JS, "tsx": JS, "java": JAVA, "go": GO, "rust": RUST, "csharp": CSHARP, "c": C, "cpp": CPP,
    "ruby": RUBY, "php": PHP, "swift": SWIFT, "scala": SCALA, "lua": LUA, "bash": BASH, "kotlin": KOTLIN,
    "powershell": POWERSHELL, "objc": OBJC, "dart": DART,
}

# что разбор языка даёт: s — символы, c — вызовы, i — импорты, b — базовые классы
CAPABILITIES: dict[str, str] = {
    "javascript": "scib", "typescript": "scib", "tsx": "scib", "java": "scib", "go": "sci", "rust": "scib", "csharp": "scib", "c": "sci",
    "cpp": "scib", "ruby": "scib", "php": "scib", "swift": "scib", "scala": "scib", "lua": "sci", "bash": "sci",
    "kotlin": "scib", "powershell": "sci", "objc": "scib", "dart": "sib", "groovy": "sib",
}


def spec_for(language: Language) -> Spec | None:
    return SPECS.get(language.id)
