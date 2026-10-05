"""Построение графа проекта: разбор файлов, привязка вызовов и импортов, адаптеры стека, история, тесты, подсистемы.

`build_graph(root, config)` возвращает граф в формате схемы (`schema`): узлы `file`, `symbol`, `ui`, `route`, `table`, `env`, `service`,
`commit` и рёбра `defines`, `calls`, `calls_ref`, `imports`, `inherits`, `covers`, `changed`, а от адаптеров — `handles`, `reads`,
`writes`, `reads_env`, `calls_api`, `refers_to`, `runs`. Метрики дописывает `metrics.enrich` отдельным шагом.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from projectmap.adapters import AdapterContext, run_adapters
from projectmap.config import Config
from projectmap.context import tracked_files
from projectmap.extract import extract_file
from projectmap.graphbuf import Edges, enclosing_function, file_id, read_text, symbol_id, ui_id  # noqa: F401 (повторный экспорт)
from projectmap.history import read_history
from projectmap.imports import FileIndex
from projectmap.languages import language_of
from projectmap.model import MODULE_CALLER, REF, Call, FileFacts
from projectmap.paths import is_test_path
from projectmap.resolve import Resolver
from projectmap.schema import SCHEMA_VERSION

ID_REFERENCES = (re.compile(r"getElementById\(\s*['\"]([\w-]+)['\"]"), re.compile(r"querySelector(?:All)?\(\s*['\"]#([\w-]+)"),
                 re.compile(r"\$\(\s*['\"]#?([\w-]+)['\"]\s*\)"))
WEB_LANGUAGES = frozenset({"javascript", "typescript", "tsx", "html"})


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


def _link_id_references(facts: dict[str, FileFacts], texts: dict[str, str], nodes: dict[str, dict[str, Any]], edges: Edges) -> None:
    """Скрипт обращается к элементу по `id` (`getElementById`, `querySelector('#x')`, `$('x')`): элемент -> функция, которая с ним работает."""
    by_id: dict[str, list[str]] = {}
    for node_id, node in nodes.items():
        if node["kind"] == "ui":
            by_id.setdefault(node["label"], []).append(node_id)
    for path, text in texts.items():
        for number, line in enumerate(text.splitlines(), 1):
            for pattern in ID_REFERENCES:
                for match in pattern.finditer(line):
                    candidates = by_id.get(match.group(1), [])
                    same_file = [c for c in candidates if nodes[c]["path"] == path]
                    chosen = same_file or (candidates if len(candidates) == 1 else [])
                    symbol = enclosing_function(facts[path], number)
                    owner = symbol_id(path, symbol.qualname) if symbol else file_id(path)
                    for ui in chosen:
                        edges.add(ui, owner, "handled_by", number, how="id")


def build_graph(root: Path, config: Config | None = None, *, history_limit: int = 500) -> dict[str, Any]:
    config = config or Config()
    started = time.time()
    tracked = [p for p in tracked_files(root) if not config.is_ignored(p)]
    facts: dict[str, FileFacts] = {}
    web_texts: dict[str, str] = {}
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
        if language.id in WEB_LANGUAGES:
            web_texts[path] = text

    nodes: dict[str, dict[str, Any]] = {}
    edges = Edges()
    groups = {path: group_of(path, config) for path in facts}
    for path, file_facts in facts.items():
        language = language_of(path)
        nodes[file_id(path)] = {"id": file_id(path), "kind": "file", "label": path.rsplit("/", 1)[-1], "sub": language.id if language else
                                (path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else "file"),
                                "path": path, "line": None, "group": groups[path], "layer": "file"}
        for symbol in file_facts.symbols:
            if symbol.kind == "module":
                if symbol.doc:
                    nodes[file_id(path)]["doc"] = symbol.doc[:240]
                continue
            sid = symbol_id(path, symbol.qualname)
            nodes[sid] = {"id": sid, "kind": "symbol", "label": symbol.name, "sub": symbol.kind, "path": path, "line": symbol.line,
                          "group": groups[path], "layer": "symbol", "lang": symbol.lang, "sig": symbol.signature[:160]}
            if symbol.doc:
                nodes[sid]["doc"] = symbol.doc[:240]
            edges.add(file_id(path), sid, "defines", symbol.line)
        for element in file_facts.elements:
            uid = ui_id(path, element.id)
            nodes[uid] = {"id": uid, "kind": "ui", "label": element.id, "sub": element.tag, "path": path, "line": element.line,
                          "group": groups[path], "layer": "ui", "text": element.label}

    resolver = Resolver(facts, FileIndex(set(facts)))
    how_counts: Counter[str] = Counter()
    for path, file_facts in facts.items():
        test_file = is_test_path(path)
        for imp_target, count in resolver.import_edges(path):
            edges.add(file_id(path), file_id(imp_target), "imports", n=count)
            if test_file and not is_test_path(imp_target):  # слабое покрытие: тест подключает файл кода, даже если вызовы не разобрались
                edges.add(file_id(path), file_id(imp_target), "covers", how="import")
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
        for element in file_facts.elements:
            for handler in element.handlers:
                head, _, last = handler.rpartition(".")
                found = resolver.resolve_call(Call(path, MODULE_CALLER, last, head, element.line))
                if found.symbol is not None:
                    edges.add(ui_id(path, element.id), symbol_id(found.symbol.path, found.symbol.qualname), "handled_by", element.line, how=found.how)
        for symbol in file_facts.symbols:
            for base in symbol.bases:
                found = resolver.resolve_base(path, base)
                if found.symbol is not None and (found.symbol.path, found.symbol.qualname) != (path, symbol.qualname):
                    edges.add(symbol_id(path, symbol.qualname), symbol_id(found.symbol.path, found.symbol.qualname), "inherits", symbol.line, how=found.how)
    _link_id_references(facts, web_texts, nodes, edges)

    notes: list[str] = []
    adapter_ctx = AdapterContext(root, config, facts, nodes, edges, groups, resolver, notes)
    ran = run_adapters(adapter_ctx, config.adapters or None)

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
    notes.insert(0, f"вызовы: точных {exact}, по единственному имени {how_counts['unique']}, неоднозначных (не связаны) {how_counts['ambiguous']}, "
                    f"внешних {how_counts['external']}")
    if ran:
        notes.append("адаптеры: " + ", ".join(ran))
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
