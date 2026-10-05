"""Разбор по регулярным выражениям для языков, у которых грамматика tree-sitter непригодна (сейчас — Groovy).

Даёт классы, методы, импорты и базовые классы; вызовы не извлекаются. Метод относится к ближайшему классу выше по тексту
с меньшим отступом. Это упрощённый разбор: его пределы записаны в `CAPABILITIES` (`sib`, без вызовов).
"""

from __future__ import annotations

import re

from projectmap.languages import Language
from projectmap.model import FileFacts, Import, Symbol
from projectmap.paths import is_test_path

MODIFIERS = r"(?:(?:public|private|protected|abstract|final|static|synchronized|@\w+)\s+)*"
CLASS = re.compile(rf"^(?P<indent>\s*){MODIFIERS}(?P<kind>class|interface|enum|trait)\s+(?P<name>\w+)"
                   r"(?:\s+extends\s+(?P<extends>[\w.]+))?(?:\s+implements\s+(?P<implements>[\w.,\s]+?))?\s*\{")
METHOD = re.compile(rf"^(?P<indent>\s*){MODIFIERS}(?:def|void|boolean|int|long|double|String|[A-Z][\w.<>\[\]]*)\s+(?P<name>\w+)\s*\([^)]*\)\s*(?:throws\s+[\w.,\s]+)?\{{")
IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?(?P<module>[\w.]+(?:\.\*)?)(?:\s+as\s+(?P<alias>\w+))?", re.M)
DEF_FUNCTION = re.compile(r"^def\s+(?P<name>\w+)\s*\([^)]*\)\s*\{", re.M)


def extract_groovy(path: str, text: str, language: Language) -> FileFacts:
    lines = text.splitlines()
    facts = FileFacts(path, language.id, language.tier, lines=len(lines))
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    facts.symbols.append(Symbol(stem, path, "module", path, 1, max(len(lines), 1), lang=language.id))
    for match in IMPORT.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        alias = match.group("alias")
        module = match.group("module")
        facts.imports.append(Import(path, module, [], {alias: module.rsplit(".", 1)[-1]} if alias else {}, line))
    stack: list[tuple[int, str]] = []  # (отступ, имя класса)
    for number, line in enumerate(lines, 1):
        cls = CLASS.match(line)
        if cls:
            indent = len(cls.group("indent"))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            qualname = ".".join([name for _, name in stack] + [cls.group("name")])
            bases = [b for b in [cls.group("extends")] if b] + [b.strip() for b in (cls.group("implements") or "").split(",") if b.strip()]
            facts.symbols.append(Symbol(cls.group("name"), qualname, cls.group("kind").replace("trait", "trait"), path, number, number,
                                        signature=line.strip()[:140], bases=bases, lang=language.id))
            stack.append((indent, cls.group("name")))
            continue
        method = METHOD.match(line) or (DEF_FUNCTION.match(line) if not stack else None)
        if method and method.group("name") not in ("if", "for", "while", "switch", "catch", "return"):
            indent = len(method.groupdict().get("indent") or "")
            while stack and stack[-1][0] >= indent and indent == 0:
                stack.pop()
            owner = [name for _, name in stack]
            qualname = ".".join(owner + [method.group("name")])
            kind = "test" if is_test_path(path) else ("method" if owner else "function")
            facts.symbols.append(Symbol(method.group("name"), qualname, kind, path, number, number, signature=line.strip()[:140], lang=language.id))
    return facts
