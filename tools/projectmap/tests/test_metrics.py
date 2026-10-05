"""Метрики графа: группы Louvain без тестов, fan-in/out, хабы, согласие с подсистемами, правки и co-change, ключевые слова."""

from __future__ import annotations

import copy

from projectmap.keywords import file_keywords, split_identifier
from projectmap.metrics import MetricsOptions, enrich, normalized_mutual_information

A = ["src/a/a1.py", "src/a/a2.py", "src/a/a3.py", "src/a/a4.py"]
B = ["src/b/b1.py", "src/b/b2.py", "src/b/b3.py", "src/b/b4.py"]


def _fid(path):
    return f"file:{path}"


def _sid(path):
    return f"sym:{path}::f"


def make_graph():
    """Две плотные группы файлов A и B, один мост, тест, ссылающийся на обе, документ без связей и пять коммитов."""
    nodes, edges = [], []
    for path in A + B + ["tests/test_all.py", "docs/readme.md"]:
        group = "alpha" if path in A else "beta" if path in B else "other"
        nodes.append({"id": _fid(path), "kind": "file", "path": path, "label": path.rsplit("/", 1)[-1], "group": group})
        if path.endswith(".py"):
            stem = path.rsplit("/", 1)[-1][:-3]
            nodes.append({"id": _sid(path), "kind": "symbol", "path": path, "label": f"load_{stem}_invoice" if path == A[0] else "run_job"})
            edges.append({"from": _fid(path), "to": _sid(path), "rel": "defines", "n": 1})

    def call(a, b, n=1):
        edges.append({"from": _sid(a), "to": _sid(b), "rel": "calls", "n": n})

    for group in (A, B):
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                call(a, b, 3)
    call(A[0], B[0])  # мост между группами
    call("tests/test_all.py", A[1], 9)  # тест сшивает группы, но в группы не входит
    call("tests/test_all.py", B[1], 9)
    for index, files in enumerate([(A[0], A[1], "docs/readme.md"), (A[0], A[1], "docs/readme.md"), (A[0], A[1], "docs/readme.md"),
                                   (A[0], B[3]), (A[2],)]):
        commit = f"commit:c{index}"
        nodes.append({"id": commit, "kind": "commit", "label": f"c{index}"})
        edges += [{"from": commit, "to": _fid(f), "rel": "changed", "n": 1} for f in files]
    return {"nodes": nodes, "edges": edges}


def _node(graph, path):
    return next(n for n in graph["nodes"] if n["id"] == _fid(path))


def test_two_dense_groups_are_found_and_tests_stay_out():
    graph = make_graph()
    summary = enrich(graph)
    groups = {path: _node(graph, path).get("community") for path in A + B}
    assert len({groups[p] for p in A}) == 1 and len({groups[p] for p in B}) == 1 and groups[A[0]] != groups[B[0]]
    assert "community" not in _node(graph, "tests/test_all.py") and "community" not in _node(graph, "docs/readme.md")
    assert len(summary["communities"]) == 2 and summary["modularity"] > 0.3
    assert summary["files_in_communities"] == 8 and graph["metrics"] is summary


def test_result_is_deterministic():
    first, second = make_graph(), make_graph()
    enrich(first)
    enrich(second)
    assert first == second


def test_communities_are_numbered_by_size_then_name():
    graph = make_graph()
    graph["nodes"].append({"id": _fid("src/c/c1.py"), "kind": "file", "path": "src/c/c1.py", "label": "c1.py", "group": "gamma"})
    graph["nodes"].append({"id": "sym:c1", "kind": "symbol", "path": "src/c/c1.py", "label": "x"})
    graph["edges"] += [{"from": _fid("src/c/c1.py"), "to": "sym:c1", "rel": "defines"}, {"from": "sym:c1", "to": _sid(B[3]), "rel": "calls"}]
    enrich(graph)
    sizes = [c["size"] for c in graph["metrics"]["communities"]]
    assert sizes == sorted(sizes, reverse=True)


def test_fan_in_fan_out_instability_and_hubs():
    graph = make_graph()
    summary = enrich(graph, MetricsOptions(hub_min=1))
    a1, a4 = _node(graph, A[0]), _node(graph, A[3])
    assert (a1["fan_in"], a1["fan_out"]) == (0, 4) and a1["instability"] == 1.0  # 3 в группе + мост в B
    assert (a4["fan_in"], a4["fan_out"]) == (3, 0) and a4["instability"] == 0.0
    assert {h["path"] for h in summary["hubs"]} == {A[1], A[2], B[0], B[1], B[2]}  # b1 получает мост из a1 и вызывает троих
    assert summary["top_fan_in"][0]["value"] >= summary["top_fan_in"][-1]["value"]
    symbol = next(n for n in graph["nodes"] if n["id"] == _sid(A[0]))
    assert symbol["fan_out"] == 4


def test_agreement_with_groups_and_misplaced_files():
    graph = make_graph()
    assert enrich(graph)["agreement_with_groups"] == 1.0
    shifted = make_graph()
    next(n for n in shifted["nodes"] if n["id"] == _fid(A[0]))["group"] = "beta"
    summary = enrich(shifted, MetricsOptions(misplaced_min_size=3))
    assert summary["agreement_with_groups"] < 1.0
    assert {"path": A[0], "group": "beta", "community": summary["misplaced"][0]["community"], "community_group": "alpha"} in summary["misplaced"]


def test_nmi_extremes():
    same = {"a": "x", "b": "x", "c": "y", "d": "y"}
    assert normalized_mutual_information(same, {"a": 0, "b": 0, "c": 1, "d": 1}) == 1.0
    assert normalized_mutual_information(same, {"a": 0, "b": 1, "c": 0, "d": 1}) == 0.0
    assert normalized_mutual_information({}, {}) == 0.0


def test_churn_and_cochange_edges_and_hidden_coupling():
    graph = make_graph()
    summary = enrich(graph)
    assert _node(graph, A[0])["churn"] == 4 and _node(graph, A[2])["churn"] == 1
    pairs = [e for e in graph["edges"] if e["rel"] == "co_changes"]
    assert {(e["from"], e["to"]): e["n"] for e in pairs}[(_fid("docs/readme.md"), _fid(A[0]))] == 3
    assert summary["cochange_pairs"] == 3  # a1+a2, a1+readme, a2+readme
    hidden = {frozenset((h["a"], h["b"])) for h in summary["cochange_hidden"]}
    assert frozenset((A[0], "docs/readme.md")) in hidden and frozenset((A[0], A[1])) not in hidden  # a1-a2 связаны кодом
    assert summary["top_churn"][0] == {"path": A[0], "commits": 4}


def test_huge_commits_do_not_create_cochange():
    graph = make_graph()
    graph["nodes"].append({"id": "commit:big", "kind": "commit", "label": "big"})
    graph["edges"] += [{"from": "commit:big", "to": _fid(p), "rel": "changed"} for p in A + B]
    summary = enrich(graph, MetricsOptions(cochange_max_files=5))
    assert summary["cochange_pairs"] == 3


def test_enrich_is_idempotent_for_cochange_edges():
    graph = make_graph()
    enrich(graph)
    count = sum(1 for e in graph["edges"] if e["rel"] == "co_changes")
    enrich(graph)
    assert sum(1 for e in graph["edges"] if e["rel"] == "co_changes") == count


def test_split_identifier_handles_snake_camel_and_stop_words():
    assert split_identifier("load_user_Invoice") == ["load", "user", "invoice"]
    assert split_identifier("getHTTPResponse2") == ["http", "response"]
    assert split_identifier("get_self_x") == []


def test_keywords_prefer_words_rare_in_other_files():
    graph = make_graph()
    keywords = file_keywords(graph)
    assert "invoice" in keywords[_fid(A[0])] and _fid(B[0]) not in keywords or "invoice" not in keywords.get(_fid(B[0]), [])
    other = copy.deepcopy(graph)
    enrich(other)
    assert _node(other, A[0])["keywords"][0] == "invoice"
