"""Привязка импортов к файлам проекта: какой файл подключает строка `import` на каждом языке.

Результат — список целей (`Target`): локальное имя в файле, исходное имя в целевом файле, сам файл и признак «это модуль»
(тогда обращение `имя.функция()` ведёт в функцию модуля, иначе — в статический член класса). Внешние пакеты (стандартная
библиотека, зависимости) целей не дают: их в проекте нет, вызовы к ним остаются внешними.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from projectmap.languages import language_of
from projectmap.model import Import


@dataclass(frozen=True)
class Target:
    local: str  # имя, под которым цель видна в файле
    original: str | None  # исходное имя в целевом файле (None — импортирован сам модуль)
    file: str
    is_module: bool


class FileIndex:
    """Файлы проекта с поиском по хвосту пути на границе каталогов."""

    def __init__(self, files: list[str] | set[str]) -> None:
        self.files = set(files)
        self._by_basename: dict[str, list[str]] = {}
        for path in sorted(self.files):
            self._by_basename.setdefault(path.rsplit("/", 1)[-1], []).append(path)

    def find_suffix(self, suffix: str) -> list[str]:
        suffix = suffix.strip("/")
        if not suffix:
            return []
        if suffix in self.files:
            return [suffix]
        return [p for p in self._by_basename.get(suffix.rsplit("/", 1)[-1], []) if p.endswith("/" + suffix)]

    def in_directory(self, directory_suffix: str, extensions: tuple[str, ...]) -> list[str]:
        """Файлы каталога, путь которого оканчивается на `directory_suffix` (пакет Go, каталог модуля)."""
        directory_suffix = directory_suffix.strip("/")
        found = []
        for path in sorted(self.files):
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
            if path.endswith(extensions) and (folder == directory_suffix or folder.endswith("/" + directory_suffix)):
                found.append(path)
        return found


def _common_depth(a: str, b: str) -> int:
    """Сколько начальных каталогов у путей общие."""
    parts_a = a.rsplit("/", 1)[0].split("/") if "/" in a else []
    parts_b = b.rsplit("/", 1)[0].split("/") if "/" in b else []
    depth = 0
    for x, y in zip(parts_a, parts_b):
        if x != y:
            break
        depth += 1
    return depth


def _pick(candidates: list[str], importer: str) -> list[str]:
    """Несколько кандидатов: берём лежащий ближе к импортирующему файлу; если ближайший не один — никого (не гадаем)."""
    unique = list(dict.fromkeys(candidates))
    if len(unique) <= 1:
        return unique
    ranked = sorted(unique, key=lambda c: (-_common_depth(importer, c), len(c), c))
    return [ranked[0]] if _common_depth(importer, ranked[0]) > _common_depth(importer, ranked[1]) else []


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


# ───────────────────────────── Python ─────────────────────────────

def _python_files(importer: str, module: str, index: FileIndex) -> list[str]:
    """Файлы модуля `a.b.c` (абсолютный или относительный): `a/b/c.py` или `a/b/c/__init__.py`."""
    if module.startswith("."):
        level = len(module) - len(module.lstrip("."))
        rest = module.lstrip(".").replace(".", "/")
        base = importer.rsplit("/", 1)[0] if "/" in importer else ""
        parts = base.split("/") if base else []
        parts = parts[: max(len(parts) - (level - 1), 0)]
        prefix = "/".join(parts)
        stem = f"{prefix}/{rest}".strip("/") if rest else prefix
        found = [p for p in (f"{stem}.py", f"{stem}/__init__.py", f"{stem}.pyi") if p in index.files] if stem else \
                [p for p in ("__init__.py",) if p in index.files]
        return found[:1]
    dotted = module.replace(".", "/")
    candidates = index.find_suffix(f"{dotted}.py") + index.find_suffix(f"{dotted}/__init__.py")
    return _pick(candidates, importer)


def _python_targets(importer: str, imp: Import, index: FileIndex) -> list[Target]:
    targets: list[Target] = []
    module_files = _python_files(importer, imp.module, index)
    if not imp.names:  # `import a.b.c` / `import a.b as ab`
        if module_files:
            full, alias = imp.module, next((k for k, v in imp.aliases.items() if v == imp.module), None)
            for local in dict.fromkeys([alias or imp.module.rsplit(".", 1)[-1], full]):
                targets.append(Target(local, None, module_files[0], True))
        return targets
    for name in imp.names:
        local = next((k for k, v in imp.aliases.items() if v == name), name)
        sub_module = _python_files(importer, f"{imp.module}.{name}" if not imp.module.endswith(".") else f"{imp.module}{name}", index)
        if sub_module:  # `from pkg import module`
            targets.append(Target(local, None, sub_module[0], True))
        elif module_files:  # `from module import name`
            targets.append(Target(local, name, module_files[0], False))
    return targets


# ───────────────────────────── JavaScript / TypeScript ─────────────────────────────

JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _js_files(importer: str, module: str, index: FileIndex) -> list[str]:
    if not module.startswith("."):
        return []  # пакет из node_modules или алиас сборщика
    base = importer.rsplit("/", 1)[0] if "/" in importer else ""
    target = posixpath.normpath(posixpath.join(base, module)).lstrip("./") if base else posixpath.normpath(module).lstrip("./")
    if target in index.files:
        return [target]
    for suffix in JS_EXTENSIONS:
        if target + suffix in index.files:
            return [target + suffix]
    for suffix in JS_EXTENSIONS:
        if f"{target}/index{suffix}" in index.files:
            return [f"{target}/index{suffix}"]
    return []


def _js_targets(importer: str, imp: Import, index: FileIndex) -> list[Target]:
    files = _js_files(importer, imp.module, index)
    if not files:
        return []
    targets = [Target(imp.module.rsplit("/", 1)[-1], None, files[0], True)]
    for name in imp.names:
        if name == "default":  # `import a from './a'`: локальное имя ведёт на весь модуль
            continue
        local = next((k for k, v in imp.aliases.items() if v == name), name)
        targets.append(Target(local, name, files[0], False))
    for local, original in imp.aliases.items():
        if original == "default":
            targets.append(Target(local, None, files[0], True))
    return targets


# ───────────────────────────── Java, Kotlin, Scala, Groovy, C#, PHP ─────────────────────────────

CLASSPATH_EXTENSIONS = {"java": (".java",), "kotlin": (".kt", ".java"), "scala": (".scala", ".java"), "groovy": (".groovy", ".java"),
                        "csharp": (".cs",), "php": (".php",)}


def _classpath_targets(importer: str, imp: Import, index: FileIndex, language: str) -> list[Target]:
    """`a.b.Class` -> файл `a/b/Class.<расширение>`; `a.b.*` и пространства имён без файла класса -> каталог пакета."""
    module = imp.module.replace("\\", ".").strip(".")
    extensions = CLASSPATH_EXTENSIONS[language]
    if module.endswith(".*"):
        files = index.in_directory(module[:-2].replace(".", "/"), extensions)
        return [Target(_stem(f), _stem(f), f, False) for f in files]
    parts = module.split(".")
    for cut in range(len(parts), 0, -1):  # `import static a.b.Class.method` — класс на один-два сегмента левее
        base = "/".join(parts[:cut])
        files = [f for ext in extensions for f in index.find_suffix(base + ext)]
        if files:
            klass = parts[cut - 1]
            local = next((k for k, v in imp.aliases.items() if v == klass), klass)
            return [Target(local, klass, _pick(files, importer)[0], False)] if _pick(files, importer) else []
    return []


# ───────────────────────────── Go ─────────────────────────────

def _go_targets(importer: str, imp: Import, index: FileIndex) -> list[Target]:
    files = index.in_directory(imp.module, (".go",))
    local = next(iter(imp.aliases), imp.module.rsplit("/", 1)[-1])
    return [Target(local, None, f, True) for f in files if not f.endswith("_test.go")]


# ───────────────────────────── Rust ─────────────────────────────

def _rust_targets(importer: str, imp: Import, index: FileIndex) -> list[Target]:
    parts = [p for p in imp.module.split("::") if p not in ("crate", "self", "super")]
    if imp.module.split("::")[0] in ("std", "core", "alloc") or not parts:
        return []
    targets: list[Target] = []
    for cut in range(len(parts), 0, -1):
        base = "/".join(parts[:cut])
        files = index.find_suffix(base + ".rs") + index.find_suffix(base + "/mod.rs")
        if files:
            file = _pick(files, importer)
            if file:
                last = parts[cut - 1]
                if cut == len(parts):
                    targets.append(Target(last, None, file[0], True))
                for name in imp.names:
                    targets.append(Target(name, name, file[0], False))
                if cut < len(parts):
                    targets.append(Target(parts[-1], parts[-1], file[0], False))
            break
    return targets


# ───────────────────────────── C, C++, Objective-C ─────────────────────────────

def _c_targets(importer: str, imp: Import, index: FileIndex) -> list[Target]:
    base = importer.rsplit("/", 1)[0] if "/" in importer else ""
    relative = posixpath.normpath(posixpath.join(base, imp.module)) if base else posixpath.normpath(imp.module)
    files = [relative] if relative in index.files else _pick(index.find_suffix(imp.module), importer)
    return [Target(_stem(files[0]), None, files[0], True)] if files else []


# ───────────────────────────── Ruby, Lua, Dart, Shell, PowerShell ─────────────────────────────

def _script_targets(importer: str, imp: Import, index: FileIndex, language: str) -> list[Target]:
    base = importer.rsplit("/", 1)[0] if "/" in importer else ""
    suffixes = {"ruby": (".rb",), "lua": (".lua", "/init.lua"), "dart": (".dart",), "bash": ("", ".sh", ".bash"),
                "powershell": ("", ".ps1", ".psm1")}[language]
    module = imp.module
    if module.startswith("package:"):
        module = module.split("/", 1)[-1]
    if language == "lua":
        module = module.replace(".", "/")
    candidates: list[str] = []
    if module.startswith(".") or "/" in module or language in ("bash", "powershell"):
        relative = posixpath.normpath(posixpath.join(base, module)) if base else posixpath.normpath(module)
        candidates += [relative + s for s in suffixes if relative + s in index.files]
    if not candidates:
        candidates = [f for s in suffixes for f in index.find_suffix(module.lstrip("./") + s)]
    candidates = _pick(candidates, importer)
    return [Target(_stem(candidates[0]), None, candidates[0], True)] if candidates else []


def resolve_import(importer: str, imp: Import, index: FileIndex) -> list[Target]:
    """Цели импорта в проекте; пусто — пакет вне проекта или файл не найден."""
    language = language_of(importer)
    if language is None:
        return []
    lang = language.id
    if lang == "python":
        return _python_targets(importer, imp, index)
    if lang in ("javascript", "typescript", "tsx"):
        return _js_targets(importer, imp, index)
    if lang in CLASSPATH_EXTENSIONS:
        return _classpath_targets(importer, imp, index, lang)
    if lang == "go":
        return _go_targets(importer, imp, index)
    if lang == "rust":
        return _rust_targets(importer, imp, index)
    if lang in ("c", "cpp", "objc"):
        return _c_targets(importer, imp, index)
    if lang in ("ruby", "lua", "dart", "bash", "powershell"):
        return _script_targets(importer, imp, index, lang)
    return []
