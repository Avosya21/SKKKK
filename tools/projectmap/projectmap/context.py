"""Контекст проекта для проб: корень, граф, конфиг, список файлов и разбор графа по файлам.

Список файлов берётся из git (`git ls-files`), а вне git — обходом каталога без служебных папок.
"""

from __future__ import annotations

import json
import os
import subprocess
from functools import cached_property
from pathlib import Path
from typing import Any

from projectmap.config import Config

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".pytest_cache", "dist", "build"}
FILE_LINK_RELATIONS = {"calls", "calls_ref", "imports", "inherits", "runs", "refers_to"}


def tracked_files(root: Path) -> list[str]:
    """Файлы проекта (пути с `/` от корня): из git, а без git — обходом каталога."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        names = [p for p in out.decode("utf-8").split("\0") if p]
        if names:
            return sorted(names)
    except (OSError, subprocess.CalledProcessError):
        pass
    found: list[str] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            found.append(Path(current, name).relative_to(root).as_posix())
    return sorted(found)


def load_graph(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class ProjectContext:
    def __init__(self, root: Path, graph: dict[str, Any], config: Config) -> None:
        self.root = root
        self.graph = graph
        self.config = config

    @cached_property
    def tracked(self) -> list[str]:
        return tracked_files(self.root)

    @cached_property
    def considered(self) -> list[str]:
        """Файлы, которые карта должна знать: всё отслеживаемое, кроме осознанных исключений конфига."""
        return [p for p in self.tracked if not self.config.is_ignored(p)]

    @property
    def ignored_count(self) -> int:
        return len(self.tracked) - len(self.considered)

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8-sig", errors="replace")

    @cached_property
    def nodes(self) -> list[dict[str, Any]]:
        return self.graph.get("nodes", [])

    @cached_property
    def edges(self) -> list[dict[str, Any]]:
        return self.graph.get("edges", [])

    def nodes_of(self, kind: str) -> list[dict[str, Any]]:
        return [n for n in self.nodes if n.get("kind") == kind]

    @cached_property
    def file_node_ids(self) -> dict[str, str]:
        """Путь файла -> id узла."""
        return {n["path"]: n["id"] for n in self.nodes_of("file") if n.get("path")}

    @cached_property
    def file_ids(self) -> set[str]:
        return set(self.file_node_ids.values())

    @cached_property
    def file_of_symbol(self) -> dict[str, str]:
        """id символа -> id файла, где он определён (по связям `defines`)."""
        return {e["to"]: e["from"] for e in self.edges if e.get("rel") == "defines" and e.get("from") in self.file_ids}

    def file_of(self, node_id: str) -> str | None:
        if node_id in self.file_of_symbol:
            return self.file_of_symbol[node_id]
        return node_id if node_id in self.file_ids else None

    @cached_property
    def file_links(self) -> set[tuple[str, str]]:
        """Направленные связи между файлами (id файлов): вызовы, импорты, запуски, ссылки; связи символов сводятся к файлам."""
        links: set[tuple[str, str]] = set()
        for e in self.edges:
            if e.get("rel") in FILE_LINK_RELATIONS or str(e.get("rel", "")).startswith("m:"):
                a, b = self.file_of(e["from"]), self.file_of(e["to"])
                if a and b and a != b:
                    links.add((a, b))
        return links

    @cached_property
    def touched_by(self) -> dict[str, set[str]]:
        """Узел -> виды связей, в которых он участвует (любым концом)."""
        touched: dict[str, set[str]] = {}
        for e in self.edges:
            touched.setdefault(e["from"], set()).add(e["rel"])
            touched.setdefault(e["to"], set()).add(e["rel"])
        return touched
