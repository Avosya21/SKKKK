"""Метрики графа: группы по связности (Louvain), fan-in/out, нестабильность, «хабы», частота правок и co-change.

`enrich(graph)` дописывает метрики в узлы файлов (`community`, `fan_in`, `fan_out`, `instability`, `churn`, `keywords`),
fan-in/out в узлы символов, добавляет рёбра `co_changes` и кладёт итоги в `graph["metrics"]`.

Группы считаются по структурным связям между файлами (вызовы, импорты, наследование) **без тестов**: иначе связь «тест покрывает
код» сливает код в группы вокруг тестов. Алгоритм Louvain запускается несколько раз с фиксированными seed (результат
воспроизводим), берётся разбиение с лучшей модулярностью, группы нумеруются по убыванию размера — цвета групп стабильны.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

import networkx as nx

from projectmap.keywords import file_keywords
from projectmap.paths import is_test_path

FILE_RELATIONS = frozenset({"calls", "calls_ref", "imports", "inherits"})
SYMBOL_RELATIONS = frozenset({"calls", "calls_ref", "inherits"})
NAME_ONLY_WEIGHT = 0.5  # связь, найденная только по единственному имени (`how: unique`), весит вдвое меньше точной


@dataclass(frozen=True)
class MetricsOptions:
    resolution: float = 1.5  # крупнее — больше мелких групп
    seeds: int = 5  # сколько раз запускать Louvain
    hub_min: int = 15  # «хаб»: и fan-in, и fan-out не меньше
    cochange_min: int = 3  # пара файлов считается связанной правками, если менялась вместе не реже
    cochange_max_files: int = 15  # огромные коммиты (массовые переименования) в co-change не учитываются
    top: int = 10  # сколько строк в рейтингах
    misplaced_share: float = 0.6  # доля доминирующей подсистемы в группе, с которой файл «из чужой»
    misplaced_min_size: int = 6


def _file_of(graph_nodes: dict[str, dict[str, Any]], defined_in: dict[str, str]) -> dict[str, str]:
    """id узла -> id файла: сам файл или файл, где определён символ."""
    mapping = {i: i for i, n in graph_nodes.items() if n.get("kind") == "file"}
    mapping.update(defined_in)
    return mapping


def file_digraph(graph: dict[str, Any]) -> tuple[nx.DiGraph, dict[str, dict[str, Any]]]:
    """Направленный граф файл -> файл по структурным связям без тестов; вес — число связей между символами."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    files = {i: n for i, n in nodes.items() if n.get("kind") == "file" and not is_test_path(n.get("path", ""))}
    defined_in = {e["to"]: e["from"] for e in graph.get("edges", []) if e.get("rel") == "defines" and e.get("from") in nodes}
    owner = _file_of(nodes, defined_in)
    digraph = nx.DiGraph()
    digraph.add_nodes_from(files)
    for edge in graph.get("edges", []):
        if edge.get("rel") not in FILE_RELATIONS:
            continue
        a, b = owner.get(edge["from"]), owner.get(edge["to"])
        if a in files and b in files and a != b:
            weight = (edge.get("n", 1) or 1) * (NAME_ONLY_WEIGHT if edge.get("how") == "unique" else 1)
            if digraph.has_edge(a, b):
                digraph[a][b]["weight"] += weight
            else:
                digraph.add_edge(a, b, weight=weight)
    return digraph, files


def detect_communities(digraph: nx.DiGraph, options: MetricsOptions) -> tuple[list[set[str]], float]:
    """Группы файлов (по убыванию размера) и модулярность лучшего из нескольких запусков Louvain."""
    undirected = nx.Graph()
    undirected.add_nodes_from(digraph.nodes)
    for a, b, data in digraph.edges(data=True):
        weight = data["weight"] + (undirected[a][b]["weight"] if undirected.has_edge(a, b) else 0)
        undirected.add_edge(a, b, weight=weight)
    linked = {n for n in undirected if undirected.degree(n) > 0}
    if not linked:
        return [], 0.0
    core = undirected.subgraph(linked)
    best: tuple[float, list[set[str]]] | None = None
    for seed in range(options.seeds):
        parts = nx.community.louvain_communities(core, weight="weight", resolution=options.resolution, seed=seed)
        score = nx.community.modularity(core, parts, weight="weight")
        if best is None or score > best[0] + 1e-12:
            best = (score, parts)
    assert best is not None
    ordered = sorted((set(p) for p in best[1]), key=lambda p: (-len(p), min(p)))
    return ordered, best[0]


def normalized_mutual_information(left: dict[str, str], right: dict[str, int]) -> float:
    """Согласие двух разбиений одного множества: 1 — одинаковы, 0 — не связаны."""
    keys = [k for k in left if k in right]
    if not keys:
        return 0.0
    count = len(keys)

    def entropy(values: list[Any]) -> float:
        return -sum(c / count * math.log(c / count) for c in Counter(values).values())

    h_left, h_right = entropy([left[k] for k in keys]), entropy([right[k] for k in keys])
    h_joint = entropy([(left[k], right[k]) for k in keys])
    if h_left + h_right == 0:
        return 1.0
    return max(0.0, 2 * (h_left + h_right - h_joint) / (h_left + h_right))


def _community_summary(index: int, members: set[str], files: dict[str, dict[str, Any]], keywords: dict[str, list[str]],
                       fan_in: dict[str, int]) -> dict[str, Any]:
    groups = Counter(files[m].get("group", "") for m in members)
    dominant, share = groups.most_common(1)[0]
    words: Counter[str] = Counter()
    for member in members:
        words.update(keywords.get(member, []))
    top_files = sorted(members, key=lambda m: (-fan_in.get(m, 0), m))[:3]
    return {"id": index, "size": len(members), "group": dominant, "group_share": round(share / len(members), 2),
            "keywords": [w for w, _ in words.most_common(4)], "top_files": [files[m].get("path", m) for m in top_files]}


def _commit_files(graph: dict[str, Any], file_ids: set[str]) -> dict[str, set[str]]:
    commits: dict[str, set[str]] = {}
    for edge in graph.get("edges", []):
        if edge.get("rel") == "changed" and edge.get("to") in file_ids:
            commits.setdefault(edge["from"], set()).add(edge["to"])
    return commits


def enrich(graph: dict[str, Any], options: MetricsOptions | None = None) -> dict[str, Any]:
    """Дописать метрики в граф и вернуть итоги (они же лежат в `graph["metrics"]`)."""
    options = options or MetricsOptions()
    digraph, files = file_digraph(graph)
    all_files = {n["id"]: n for n in graph.get("nodes", []) if n.get("kind") == "file"}
    fan_in = {n: digraph.in_degree(n) for n in digraph}
    fan_out = {n: digraph.out_degree(n) for n in digraph}
    keywords = file_keywords(graph)
    communities, modularity = detect_communities(digraph, options)
    community_of = {member: i for i, members in enumerate(communities) for member in members}

    for file_id, node in all_files.items():
        if file_id in files:
            node["fan_in"], node["fan_out"] = fan_in[file_id], fan_out[file_id]
            if fan_in[file_id] + fan_out[file_id]:
                node["instability"] = round(fan_out[file_id] / (fan_in[file_id] + fan_out[file_id]), 2)
        if file_id in community_of:
            node["community"] = community_of[file_id]
        if file_id in keywords:
            node["keywords"] = keywords[file_id]

    symbol_in: Counter[str] = Counter()
    symbol_out: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    for edge in graph.get("edges", []):
        if edge.get("rel") in SYMBOL_RELATIONS and (edge["from"], edge["to"]) not in seen:
            a, b = nodes.get(edge["from"]), nodes.get(edge["to"])
            if a and b and a.get("kind") == b.get("kind") == "symbol" and not is_test_path(a.get("path", "")):
                seen.add((edge["from"], edge["to"]))
                symbol_out[edge["from"]] += 1
                symbol_in[edge["to"]] += 1
    for symbol_id, node in nodes.items():
        if node.get("kind") == "symbol" and (symbol_in[symbol_id] or symbol_out[symbol_id]):
            node["fan_in"], node["fan_out"] = symbol_in[symbol_id], symbol_out[symbol_id]

    commits = _commit_files(graph, set(all_files))
    churn = Counter(f for fs in commits.values() for f in fs)
    for file_id, count in churn.items():
        all_files[file_id]["churn"] = count
    pairs: Counter[tuple[str, str]] = Counter()
    for members in commits.values():
        if 2 <= len(members) <= options.cochange_max_files:
            ordered = sorted(members)
            for i, a in enumerate(ordered):
                for b in ordered[i + 1:]:
                    pairs[(a, b)] += 1
    linked_pairs = {frozenset((a, b)) for a, b in digraph.edges}
    cochange = [(a, b, n) for (a, b), n in pairs.items() if n >= options.cochange_min]
    existing = {(e["from"], e["to"], e["rel"]) for e in graph.get("edges", [])}
    for a, b, n in sorted(cochange):
        if (a, b, "co_changes") not in existing:
            graph.setdefault("edges", []).append({"from": a, "to": b, "rel": "co_changes", "line": 0, "n": n})

    def path(file_id: str) -> str:
        return all_files[file_id].get("path", file_id)

    group_of = {i: files[i].get("group", "") for i in community_of}
    misplaced = []
    for index, members in enumerate(communities):
        if len(members) < options.misplaced_min_size:
            continue
        dominant, count = Counter(files[m].get("group", "") for m in members).most_common(1)[0]
        if count / len(members) >= options.misplaced_share:
            misplaced += [{"path": path(m), "group": files[m].get("group", ""), "community": index, "community_group": dominant}
                          for m in sorted(members) if files[m].get("group", "") != dominant]
    summary = {
        "options": {"resolution": options.resolution, "seeds": options.seeds, "hub_min": options.hub_min},
        "files_in_graph": len(digraph), "files_in_communities": len(community_of), "edges": digraph.number_of_edges(),
        "modularity": round(modularity, 3), "communities": [_community_summary(i, m, files, keywords, fan_in) for i, m in enumerate(communities)],
        "agreement_with_groups": round(normalized_mutual_information(group_of, community_of), 2),
        "misplaced": misplaced,
        "top_fan_in": [{"path": path(n), "value": v} for n, v in sorted(fan_in.items(), key=lambda kv: (-kv[1], kv[0]))[:options.top] if v],
        "top_fan_out": [{"path": path(n), "value": v} for n, v in sorted(fan_out.items(), key=lambda kv: (-kv[1], kv[0]))[:options.top] if v],
        "hubs": [{"path": path(n), "fan_in": fan_in[n], "fan_out": fan_out[n]}
                 for n in sorted(digraph, key=lambda n: (-fan_in[n] * fan_out[n], n))
                 if fan_in[n] >= options.hub_min and fan_out[n] >= options.hub_min][:options.top],
        "top_churn": [{"path": path(n), "commits": c} for n, c in sorted(churn.items(), key=lambda kv: (-kv[1], kv[0]))[:options.top]],
        "cochange_pairs": len(cochange),
        "cochange_hidden": [{"a": path(a), "b": path(b), "commits": n}
                            for a, b, n in sorted(cochange, key=lambda x: (-x[2], x[0], x[1]))
                            if frozenset((a, b)) not in linked_pairs and frozenset((b, a)) not in linked_pairs][:options.top],
    }
    graph["metrics"] = summary
    return summary
