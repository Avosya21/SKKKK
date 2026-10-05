"""Построение графа проекта: разбор файлов, привязка вызовов и импортов, история, тесты, подсистемы.

`build_graph(root, config)` возвращает граф в формате схемы (`schema`): узлы `file`, `symbol`, `commit` и рёбра `defines`, `calls`,
`calls_ref`, `imports`, `inherits`, `covers`, `changed`. Метрики дописывает `metrics.enrich` отдельным шагом.
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Any

from projectmap.config import Config
from projectmap.context import tracked_files
from projectmap.extract import extract_file
from projectmap.history import read_history
from projectmap.imports import FileIndex
from projectmap.languages import language_of
from projectmap.model import MODULE_CALLER, REF, FileFacts, Symbol
from projectmap.paths import is_test_path
from projectmap.resolve import Resolver
from projectmap.schema import SCHEMA_VERSION

MAX_FILE_BYTES = 1_000_000
CALL_RELATIONS = {"calls", "calls_ref"}


def file_id(path: str) -> str:
    return f"file:{path}"


def symbol_id(path: str, qualname: str) -> str:
    return f"sym:{path}::{qualname}"


def group_of(path: str, config: Config) -> str:
    """Подсистема файла: по правилам `groups` конфига (самое длинное совпадение префикса), иначе первый каталог пути."""
    best = ""
    for prefix in config.groups:
        if (path == prefix or path.startswith(prefix.rstrip("/") + "/")) and len(prefix) > len(best):
            best = prefix
    if best:
        return config.groups[best]
    return path.split("/", 1)[0] if "/" in path else "корень"


def graph_stats(graph: dict[str, Any]) -> dict[str, Any]:
    by_kind = Counter(n["kind"] for n in graph["nodes"])
    by_rel = Counter(e["rel"] for e in graph["edges"])
    by_group = Counter(n["group"] for n in graph["nodes"] if n.get("group"))
    return {"nodes": len(graph["nodes"]), "edges": len(graph["edges"]), "by_kind": dict(by_kind.most_common()),
            "by_relation": dict(by_rel.most_common()), "by_group": dict(by_group.most_common())}


def read_text(root: Path, path: str) -> str | None:
    file = root / path
    try:
        if file.stat().st_size > MAX_FILE_BYTES:
            return None
        return file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


class _Edges:
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


def build_graph(root: Path, config: Config | None = None, *, history_limit: int = 500) -> dict[str, Any]:
    config = config or Config()
    started = time.time()
    tracked = [p for p in tracked_files(root) if not config.is_ignored(p)]
    facts: dict[str, FileFacts] = {}
    unreadable = 0
    for path in tracked:
        language = language_of(path)
        if language is None:
            facts[path] = FileFacts(path)
            continue
        text = read_text(root, path)
        if text is None:
            facts[path] = FileFacts(path, language.id, language.tier, error="файл не прочитан (слишком большой или не текст)")
            unreadable += 1
            continue
        facts[path] = extract_file(root, path, text)

    nodes: dict[str, dict[str, Any]] = {}
    edges = _Edges()
    groups = {path: group_of(path, config) for path in facts}
    for path, file_facts in facts.items():
        language = language_of(path)
        nodes[file_id(path)] = {"id": file_id(path), "kind": "file", "label": path.rsplit("/", 1)[-1], "sub": language.id if language else
                                (path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else "file"),
                                "path": path, "line": None, "group": groups[path], "layer": "file"}
        for symbol in file_facts.symbols:
            if symbol.kind == "module":
                continue
            sid = symbol_id(path, symbol.qualname)
            nodes[sid] = {"id": sid, "kind": "symbol", "label": symbol.name, "sub": symbol.kind, "path": path, "line": symbol.line,
                          "group": groups[path], "layer": "symbol", "lang": symbol.lang, "sig": symbol.signature[:160]}
            edges.add(file_id(path), sid, "defines", symbol.line)

    resolver = Resolver(facts, FileIndex(set(facts)))
    how_counts: Counter[str] = Counter()
    for path, file_facts in facts.items():
        test_file = is_test_path(path)
        for imp_target, count in resolver.import_edges(path):
            edges.add(file_id(path), file_id(imp_target), "imports", n=count)
        for call in file_facts.calls:
            resolution = resolver.resolve_call(call)
            how_counts[resolution.how] += 1
            if resolution.symbol is None:
                continue
            target = symbol_id(resolution.symbol.path, resolution.symbol.qualname)
            caller = file_id(path) if call.caller == MODULE_CALLER or call.caller.endswith(".<class>") else symbol_id(path, call.caller)
            if caller not in nodes:
                caller = file_id(path)
            if test_file and not is_test_path(resolution.symbol.path):
                edges.add(caller, target, "covers", call.line, how=resolution.how)
            elif not test_file:
                edges.add(caller, target, "calls_ref" if call.qualifier == REF else "calls", call.line, how=resolution.how)
        for symbol in file_facts.symbols:
            for base in symbol.bases:
                found = resolver.resolve_base(path, base)
                if found.symbol is not None and (found.symbol.path, found.symbol.qualname) != (path, symbol.qualname):
                    edges.add(symbol_id(path, symbol.qualname), symbol_id(found.symbol.path, found.symbol.qualname), "inherits", symbol.line, how=found.how)

    notes: list[str] = []
    commits = read_history(root, history_limit)
    in_graph = {file_id(p) for p in facts}
    for commit in commits:
        cid = f"commit:{commit.hash[:8]}"
        touched = [f for f in commit.files if file_id(f) in in_graph]
        if not touched:
            continue
        nodes[cid] = {"id": cid, "kind": "commit", "label": commit.subject[:120], "sub": commit.date[:10], "path": "", "line": None,
                      "group": "", "layer": "commit", "author": commit.author, "date": commit.date[:19]}
        for f in touched:
            edges.add(cid, file_id(f), "changed")
    exact = sum(how_counts[k] for k in ("local", "self", "package", "import", "module", "class"))
    notes.append(f"вызовы: точных {exact}, по единственному имени {how_counts['unique']}, неоднозначных (не связаны) {how_counts['ambiguous']}, "
                 f"внешних {how_counts['external']}")
    if unreadable:
        notes.append(f"файлов не прочитано (большие или не текст): {unreadable}")
    failed = [p for p, f in facts.items() if f.error and not f.error.startswith("в коде есть участки")]
    if failed:
        notes.append(f"файлов с ошибкой разбора: {len(failed)}")
    graph: dict[str, Any] = {
        "version": SCHEMA_VERSION, "built_at": time.time(), "source": {"tool": "projectmap", "files": len(facts), "seconds": round(time.time() - started, 1)},
        "nodes": list(nodes.values()), "edges": sorted(edges.items.values(), key=lambda e: (e["from"], e["to"], e["rel"])), "notes": notes}
    graph["stats"] = graph_stats(graph)
    return graph
