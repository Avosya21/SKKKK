"""Адаптеры стека: знание фреймворков и среды поверх общего разбора кода.

Адаптер читает файлы проекта и добавляет в граф узлы и связи, которые не видны из одного разбора языка: веб-роуты, таблицы БД,
переменные окружения, сервисы Docker, ссылки документов на файлы, запуски скриптов, связь экранов с API. Каждый адаптер сам
определяет, есть ли его стек в проекте (`applies`), поэтому для нового проекта достаточно `adapters: auto` в конфиге.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from projectmap.config import Config
from projectmap.graphbuf import Edges, enclosing_function, file_id, read_text, symbol_id
from projectmap.model import FileFacts
from projectmap.resolve import Resolver


@dataclass
class AdapterContext:
    root: Path
    config: Config
    facts: dict[str, FileFacts]
    nodes: dict[str, dict[str, Any]]
    edges: Edges
    groups: dict[str, str]
    resolver: Resolver
    notes: list[str] = field(default_factory=list)
    _texts: dict[str, str | None] = field(default_factory=dict)

    def text(self, path: str) -> str | None:
        if path not in self._texts:
            self._texts[path] = read_text(self.root, path)
        return self._texts[path]

    def paths(self, *suffixes: str) -> list[str]:
        return sorted(p for p in self.facts if p.endswith(suffixes)) if suffixes else sorted(self.facts)

    def add_node(self, node_id: str, kind: str, label: str, path: str = "", line: int | None = None, **attrs: Any) -> str:
        node = self.nodes.get(node_id)
        if node is None:
            node = {"id": node_id, "kind": kind, "label": label, "sub": attrs.pop("sub", ""), "path": path, "line": line,
                    "group": self.groups.get(path, ""), "layer": kind}
            self.nodes[node_id] = node
        for key, value in attrs.items():
            if value not in (None, "", [], {}):
                node[key] = value
        return node_id

    def owner_of(self, path: str, line: int) -> str:
        """Узел функции, внутри которой строка; иначе узел самого файла."""
        symbol = enclosing_function(self.facts[path], line) if path in self.facts else None
        return symbol_id(path, symbol.qualname) if symbol else file_id(path)

    def imports_stack(self, *modules: str) -> bool:
        """Есть ли в проекте файл, импортирующий один из модулей (по началу имени): так адаптер узнаёт, что его стек применим."""
        return any(imp.module == m or imp.module.startswith(m + ".") for f in self.facts.values() for imp in f.imports for m in modules)


@dataclass(frozen=True)
class Adapter:
    id: str
    title: str
    applies: Callable[[AdapterContext], bool]
    run: Callable[[AdapterContext], None]


def registry() -> dict[str, Adapter]:
    """Все адаптеры по идентификатору (импорт здесь, чтобы адаптеры могли пользоваться `AdapterContext`)."""
    from projectmap import (adapter_docker, adapter_env, adapter_fastapi, adapter_markdown, adapter_scripts, adapter_sqlalchemy,
                            adapter_web)

    modules = (adapter_fastapi, adapter_sqlalchemy, adapter_env, adapter_docker, adapter_markdown, adapter_scripts, adapter_web)
    return {m.ADAPTER.id: m.ADAPTER for m in modules}


def run_adapters(ctx: AdapterContext, names: list[str] | None = None) -> list[str]:
    """Выполнить адаптеры: перечисленные в конфиге или (пусто) все, чей стек есть в проекте; возвращает идентификаторы выполненных."""
    ran: list[str] = []
    for name, adapter in registry().items():
        if names and name not in names:
            continue
        if not names and not adapter.applies(ctx):
            continue
        adapter.run(ctx)
        ran.append(name)
    return ran
