"""Привязка вызовов и базовых классов к определениям проекта, одинаково для всех языков.

Вызов связывается по порядку: тот же файл, метод своего класса (`self`/`this`), тот же пакет (для языков, где пакет — каталог),
имя из импортов файла, модуль или класс проекта, единственное определение с таким именем в проекте того же семейства языков.
Если одноимённых определений несколько и по импортам не выбрать, связь не создаётся: раньше выбирался первый по алфавиту, и
`conn.close()` превращался в вызов проектной функции `close`. Общие имена встроенных типов (`get`, `close`, `post`) на объекте
неизвестного типа остаются внешними. Способ привязки записывается в `how` ребра (`local`, `self`, `package`, `import`, `module`,
`class`, `unique`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from projectmap.imports import FileIndex, Target, resolve_import
from projectmap.languages import language_of
from projectmap.model import REF, Call, FileFacts, Symbol

COMMON_METHODS = frozenset({
    "get", "post", "put", "patch", "delete", "head", "request", "send", "recv", "close", "open", "read", "readline", "write",
    "flush", "seek", "append", "extend", "insert", "remove", "pop", "add", "update", "discard", "clear", "copy", "items", "keys",
    "values", "sort", "index", "count", "join", "split", "strip", "replace", "format", "encode", "decode", "startswith",
    "endswith", "find", "lower", "upper", "start", "stop", "run", "wait", "set", "reset", "connect", "commit", "rollback",
    "execute", "fetchone", "fetchall", "cursor", "load", "loads", "dump", "dumps", "save", "next", "exists", "mkdir", "resolve",
    "is_file", "is_dir", "unlink", "rename", "terminate", "kill", "cancel", "result", "submit", "put_nowait", "get_nowait",
    "toString", "equals", "hashCode", "length", "size", "push", "map", "filter", "forEach", "then", "catch", "emit", "on", "off",
})
SELF_QUALIFIERS = frozenset({"self", "cls", "this", "$this", "super", "base", "parent", "Self"})
SAME_PACKAGE_LANGUAGES = frozenset({"go", "java", "kotlin", "scala", "csharp", "groovy", "swift"})
CLASS_KINDS = frozenset({"class", "interface", "enum", "struct", "trait"})
FAMILIES = {"python": "py", "javascript": "web", "typescript": "web", "tsx": "web", "java": "jvm", "kotlin": "jvm", "scala": "jvm",
            "groovy": "jvm", "c": "c", "cpp": "c", "objc": "c"}


def family_of(path: str) -> str:
    """Семейство языка: вызов из Python не ведёт в JavaScript и наоборот; Java, Kotlin, Scala и Groovy между собой — могут."""
    language = language_of(path)
    return FAMILIES.get(language.id, language.id) if language else ""


def _last_segment(qualifier: str) -> str:
    return re.split(r"\.|::|->|\?\.", qualifier.strip())[-1].strip() if qualifier else ""


@dataclass(frozen=True)
class Resolution:
    symbol: Symbol | None
    how: str  # local, self, package, import, module, class, unique — связь есть; ambiguous, external — связи нет


class Resolver:
    def __init__(self, facts: dict[str, FileFacts], index: FileIndex) -> None:
        self.facts = facts
        self.index = index
        self.by_path_name: dict[tuple[str, str], Symbol] = {}
        self.by_name: dict[str, list[Symbol]] = {}
        self.by_qual: dict[str, list[Symbol]] = {}
        self.top_level_by_dir: dict[tuple[str, str], list[Symbol]] = {}
        for path, file_facts in facts.items():
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
            for symbol in file_facts.symbols:
                if symbol.kind == "module":
                    continue
                self.by_path_name[(path, symbol.qualname)] = symbol
                self.by_name.setdefault(symbol.name, []).append(symbol)
                self.by_qual.setdefault(symbol.qualname, []).append(symbol)
                if "." not in symbol.qualname:
                    self.top_level_by_dir.setdefault((folder, symbol.name), []).append(symbol)
        self._targets: dict[str, list[Target]] = {}

    def targets(self, path: str) -> list[Target]:
        """Цели всех импортов файла (с учётом расположения файла)."""
        if path not in self._targets:
            found: list[Target] = []
            for imp in self.facts[path].imports:
                found += resolve_import(path, imp, self.index)
            self._targets[path] = found
        return self._targets[path]

    def import_edges(self, path: str) -> list[tuple[str, int]]:
        """Файлы, которые импортирует файл: (путь, число импортов)."""
        counts: dict[str, int] = {}
        for imp in self.facts[path].imports:
            for target in {t.file for t in resolve_import(path, imp, self.index)}:
                if target != path:
                    counts[target] = counts.get(target, 0) + 1
        return sorted(counts.items())

    def resolve_call(self, call: Call) -> Resolution:
        path, name = call.path, call.callee
        qualifier = "" if call.qualifier in ("", REF) else call.qualifier
        q = _last_segment(qualifier)
        language = language_of(path)
        if not q:
            local = self.by_path_name.get((path, name))
            if local:
                return Resolution(local, "local")
        elif q in SELF_QUALIFIERS:
            owner = call.caller.rsplit(".", 1)[0] if "." in call.caller and not call.caller.endswith(".<class>") else call.caller.removesuffix(".<class>")
            for qual in ((f"{owner}.{name}",) if owner and owner != "<module>" else ()) + (name,):
                hit = self.by_path_name.get((path, qual))
                if hit:
                    return Resolution(hit, "self")
        if language and language.id in SAME_PACKAGE_LANGUAGES and not q:
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
            same = [s for s in self.top_level_by_dir.get((folder, name), []) if s.path != path]
            if len(same) == 1:
                return Resolution(same[0], "package")
        targets = self.targets(path)
        if not q:
            for target in targets:
                if target.local == name and not target.is_module:
                    hit = self.by_path_name.get((target.file, target.original or name))
                    if hit:
                        return Resolution(hit, "import")
        else:
            for target in targets:
                if target.local != q:
                    continue
                qual = name if target.is_module else f"{target.original}.{name}" if target.original else name
                hit = self.by_path_name.get((target.file, qual))
                if hit:
                    return Resolution(hit, "module")
            owned = self.by_qual.get(f"{q}.{name}") or []
            if len(owned) == 1:
                return Resolution(owned[0], "class")
        family = family_of(path)
        candidates = [s for s in self.by_name.get(name, []) if family_of(s.path) == family]
        if not candidates:
            return Resolution(None, "external")
        if len(candidates) == 1:
            return Resolution(None, "external") if q and name in COMMON_METHODS else Resolution(candidates[0], "unique")
        return Resolution(None, "ambiguous")

    def resolve_base(self, path: str, base: str) -> Resolution:
        """Базовый класс проекта по имени из объявления класса; базы вне проекта (библиотеки) дают `external`."""
        qualifier, _, name = base.replace("::", ".").rpartition(".")
        found = self.resolve_call(Call(path, "", name, qualifier, 0))
        if found.symbol is not None and found.symbol.kind in CLASS_KINDS:
            return found
        return Resolution(None, found.how if found.symbol is None else "external")
