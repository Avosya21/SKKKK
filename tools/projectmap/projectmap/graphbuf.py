"""Общие части построения графа: идентификаторы узлов, буфер рёбер, чтение файлов, поиск функции по строке.

Вынесены из `build`, чтобы адаптеры стека (`adapters`) могли добавлять узлы и рёбра, не импортируя сборщик.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from projectmap.model import FileFacts, Symbol

MAX_FILE_BYTES = 1_000_000
NON_FUNCTION_KINDS = ("module", "class", "interface", "enum", "struct", "trait", "constant")


def file_id(path: str) -> str:
    return f"file:{path}"


def symbol_id(path: str, qualname: str) -> str:
    return f"sym:{path}::{qualname}"


def ui_id(path: str, element_id: str) -> str:
    return f"ui:{path}#{element_id}"


def route_id(method: str, url: str) -> str:
    return f"route:{method} {url}"


def table_id(name: str) -> str:
    return f"table:{name}"


def env_id(name: str) -> str:
    return f"env:{name}"


def service_id(path: str, name: str) -> str:
    return f"service:{path}::{name}"


def read_text(root: Path, path: str) -> str | None:
    file = root / path
    try:
        if file.stat().st_size > MAX_FILE_BYTES:
            return None
        return file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def enclosing_function(file_facts: FileFacts, line: int) -> Symbol | None:
    """Самый узкий символ-функция, внутри которого находится строка."""
    best: Symbol | None = None
    for symbol in file_facts.symbols:
        if symbol.kind in NON_FUNCTION_KINDS:
            continue
        if symbol.line <= line <= symbol.end_line and (best is None or symbol.end_line - symbol.line < best.end_line - best.line):
            best = symbol
    return best


class Edges:
    """Рёбра графа: повтор той же тройки (откуда, куда, вид) увеличивает счётчик `n`, а не плодит ребро."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add(self, source: str, target: str, rel: str, line: int = 0, **attrs: Any) -> None:
        if source == target:
            return
        key = (source, target, rel)
        edge = self.items.get(key)
        if edge is None:
            self.items[key] = {"from": source, "to": target, "rel": rel, "line": line, "n": 1, **attrs}
        else:
            edge["n"] += 1
            if not edge["line"] and line:
                edge["line"] = line
