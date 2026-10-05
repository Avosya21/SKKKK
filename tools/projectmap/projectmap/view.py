"""Данные для просмотрщика: граф проекта, сведённый к файлам, с метриками, раскладкой, историей правок и карточками.

`build_view(graph)` берёт готовый граф (`build_graph` + `enrich`) и возвращает словарь, который целиком встраивается в страницу
просмотрщика или записывается в json. Узлы — файлы; связи между символами сведены в связи между файлами с разбивкой по видам;
координаты посчитаны заранее (`layout`); история правок — по дням, чтобы шкала времени работала без обращения к git.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Any

from projectmap.layout import layout_nodes
from projectmap.paths import is_test_path

VIEW_VERSION = 1
EDGE_RELATIONS = ("calls", "calls_ref", "imports", "inherits", "runs", "renders", "refers_to", "covers")
LAYOUT_RELATIONS = frozenset({"calls", "calls_ref", "imports", "inherits", "runs", "renders"})  # раскладку определяют структурные связи
TOP_ITEMS = 6


def _owner_map(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, str]:
    """id любого узла -> id файла, которому он принадлежит."""
    owner = {i: i for i, n in nodes.items() if n.get("kind") == "file"}
    for edge in edges:
        if edge.get("rel") == "defines" and edge["from"] in owner:
            owner[edge["to"]] = edge["from"]
    for i, node in nodes.items():
        path = node.get("path")
        if i not in owner and path and f"file:{path}" in nodes:
            owner[i] = f"file:{path}"
    return owner


def _cards(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]], owner: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Что файл содержит и с чем связан: символы, роуты, таблицы, переменные, тесты, документы."""
    card: dict[str, dict[str, Any]] = defaultdict(lambda: {"symbols": 0, "ui": 0, "routes": [], "reads": set(), "writes": set(), "env": set(), "tests": set(), "docs": set(), "top": []})
    symbols_by_file: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for i, node in nodes.items():
        file = owner.get(i)
        if not file or node.get("kind") == "file":
            continue
        if node["kind"] == "symbol":
            card[file]["symbols"] += 1
            symbols_by_file[file].append((node.get("fan_in", 0), node.get("label", "")))
        elif node["kind"] == "ui":
            card[file]["ui"] += 1
    for edge in edges:
        rel, source, target = edge.get("rel"), edge["from"], edge["to"]
        file = owner.get(source)
        if rel == "handles" and nodes.get(source, {}).get("kind") == "route" and owner.get(target):
            card[owner[target]]["routes"].append(nodes[source]["label"])
        elif rel in ("reads", "writes") and file and nodes.get(target, {}).get("kind") == "table":
            card[file]["reads" if rel == "reads" else "writes"].add(nodes[target]["label"])
        elif rel == "reads_env" and file and target in nodes:
            card[file]["env"].add(nodes[target]["label"])
        elif rel == "covers" and file and owner.get(target) and owner[target] != file:
            card[owner[target]]["tests"].add(nodes[file]["path"])
        elif rel == "refers_to" and file and nodes.get(file, {}).get("sub") in ("markdown", "md") and target in nodes:
            card[target]["docs"].add(nodes[file]["path"])
    for file, entry in card.items():
        entry["top"] = [name for _, name in sorted(symbols_by_file.get(file, []), reverse=True)[:TOP_ITEMS] if name]
        for key in ("reads", "writes", "env", "tests", "docs"):
            entry[key] = sorted(entry[key])[:TOP_ITEMS * 2]
        entry["routes"] = sorted(entry["routes"])[:TOP_ITEMS * 2]
    return card


def _history(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]) -> tuple[list[str], dict[str, list[list[int]]], list[dict[str, Any]]]:
    """Дни с правками, правки каждого файла по дням ([номер дня, число правок]) и самые заметные коммиты."""
    day_of = {i: (n.get("date") or "")[:10] for i, n in nodes.items() if n.get("kind") == "commit" and n.get("date")}
    days = sorted(set(day_of.values()))
    day_index = {d: k for k, d in enumerate(days)}
    per_file: dict[str, Counter] = defaultdict(Counter)
    size: Counter = Counter()
    for edge in edges:
        if edge.get("rel") == "changed" and edge["from"] in day_of and edge["to"] in nodes:
            per_file[edge["to"]][day_index[day_of[edge["from"]]]] += edge.get("n", 1) or 1
            size[edge["from"]] += 1
    big = [{"day": day_index[day_of[c]], "label": nodes[c]["label"], "files": count} for c, count in size.most_common(12)]
    return days, {f: sorted([d, n] for d, n in c.items()) for f, c in per_file.items()}, big


def build_view(graph: dict[str, Any], *, title: str = "", ide: str = "", root: str = "") -> dict[str, Any]:
    """`ide` — шаблон ссылки «открыть в редакторе» с полями {root}, {path}, {line}; пусто — ссылок нет."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    edges = graph.get("edges", [])
    owner = _owner_map(nodes, edges)
    files = {i: n for i, n in nodes.items() if n.get("kind") == "file"}
    pair: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for edge in edges:
        rel = edge.get("rel")
        if rel not in EDGE_RELATIONS:
            continue
        a, b = owner.get(edge["from"]), owner.get(edge["to"])
        if a in files and b in files and a != b:
            pair[(a, b)][rel] += edge.get("n", 1) or 1
    structure = [(a, b, float(sum(c[r] for r in LAYOUT_RELATIONS))) for (a, b), c in pair.items() if any(c[r] for r in LAYOUT_RELATIONS)]
    group_of = {i: (n.get("group") or "—") for i, n in files.items()}
    positions, circles = layout_nodes(group_of, structure, {i: n.get("fan_in", 0) for i, n in files.items()})
    cards = _cards(nodes, edges, owner)
    days, per_file, big_commits = _history(nodes, edges)
    members = Counter(group_of.values())
    groups = [{"id": g, "count": members[g], "x": circles[g][0], "y": circles[g][1], "r": circles[g][2]} for g in sorted(members, key=lambda g: (-members[g], g))]
    view_nodes = []
    for i, node in sorted(files.items()):
        x, y = positions[i]
        card = cards.get(i, {})
        view_nodes.append({"id": i, "label": node.get("label", i), "path": node.get("path", ""), "kind": node.get("sub", ""), "group": group_of[i],
                           "community": node.get("community"), "fan_in": node.get("fan_in", 0), "fan_out": node.get("fan_out", 0),
                           "instability": node.get("instability"), "churn": node.get("churn", 0), "keywords": node.get("keywords", []),
                           "test": is_test_path(node.get("path", "")), "x": x, "y": y, "ch": per_file.get(i, []),
                           "ide": ide.format(root=root, path=node.get("path", ""), line=1) if ide else "",
                           "card": {k: v for k, v in card.items() if v}})
    view_edges = [{"s": a, "t": b, "w": sum(c.values()), "rels": dict(c)} for (a, b), c in sorted(pair.items())]
    metrics = graph.get("metrics") or {}
    stats = graph.get("stats") or {}
    communities = [{"id": c["id"], "size": c["size"], "group": c.get("group", ""), "share": c.get("group_share"), "keywords": c.get("keywords", []),
                    "top": c.get("top_files", [])} for c in metrics.get("communities", [])]
    return {"version": VIEW_VERSION, "title": title or "Карта проекта", "built": time.strftime("%Y-%m-%d %H:%M", time.localtime(graph.get("built_at") or time.time())),
            "groups": groups, "nodes": view_nodes, "edges": view_edges, "communities": communities, "days": days, "commits": big_commits,
            "stats": {"nodes": stats.get("nodes"), "edges": stats.get("edges"), "by_kind": stats.get("by_kind", {}), "by_relation": stats.get("by_relation", {}),
                      "modularity": metrics.get("modularity"), "agreement": metrics.get("agreement_with_groups"),
                      "hubs": metrics.get("hubs", []), "misplaced": metrics.get("misplaced", []), "top_fan_in": metrics.get("top_fan_in", []),
                      "top_churn": metrics.get("top_churn", []), "cochange_hidden": metrics.get("cochange_hidden", [])[:10]}}
