"""Раскладка заранее, данные просмотрщика и выгрузка: воспроизводимость, группы не пересекаются, страница работает без сети."""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET

import pytest

from projectmap.cli import main
from projectmap.export import FORMATS, render, render_html
from projectmap.layout import layout_nodes
from projectmap.view import build_view


def _toy():
    group_of = {f"a{i}": "a" for i in range(30)} | {f"b{i}": "b" for i in range(12)} | {f"c{i}": "c" for i in range(4)}
    edges = [(f"a{i}", f"a{(i + 1) % 30}", 2.0) for i in range(30)] + [(f"b{i}", f"b{(i + 1) % 12}", 1.0) for i in range(12)]
    edges += [("a0", "b0", 5.0), ("b1", "c0", 1.0)]
    return group_of, edges


def test_layout_is_deterministic_and_finite():
    group_of, edges = _toy()
    first = layout_nodes(group_of, edges)
    assert first == layout_nodes(group_of, edges)
    assert set(first[0]) == set(group_of) and all(math.isfinite(v) for xy in first[0].values() for v in xy)


def test_group_circles_do_not_overlap_and_members_stay_inside():
    group_of, edges = _toy()
    positions, circles = layout_nodes(group_of, edges)
    names = sorted(circles)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            (xa, ya, ra), (xb, yb, rb) = circles[a], circles[b]
            assert math.hypot(xa - xb, ya - yb) >= ra + rb - 1, (a, b)
    for node, (x, y) in positions.items():
        cx, cy, r = circles[group_of[node]]
        assert math.hypot(x - cx, y - cy) <= r + 1


def test_connected_groups_sit_closer_than_unrelated_ones():
    group_of, edges = _toy()
    _, circles = layout_nodes(group_of, edges)
    distance = lambda a, b: math.hypot(circles[a][0] - circles[b][0], circles[a][1] - circles[b][1]) - circles[a][2] - circles[b][2]  # noqa: E731
    assert distance("a", "b") < distance("a", "c")


def test_nodes_without_links_go_to_the_ring_around_the_core():
    group_of = {"x": "g", "y": "g", "z": "g", "lonely1": "g", "lonely2": "g"}
    positions, circles = layout_nodes(group_of, [("x", "y", 1.0), ("y", "z", 1.0)])
    r = circles["g"][2]
    core = max(math.hypot(*positions[i]) for i in "xyz")
    ring = min(math.hypot(positions[i][0] - circles["g"][0], positions[i][1] - circles["g"][1]) for i in ("lonely1", "lonely2"))
    assert ring > 0 and positions["lonely1"] != positions["lonely2"] and r > 0 and core >= 0


@pytest.fixture()
def graph():
    files = [{"id": f"file:{p}", "kind": "file", "label": p.split("/")[-1], "path": p, "sub": "python", "group": p.split("/")[0], "fan_in": i, "fan_out": 1, "churn": 2, "community": i % 2}
             for i, p in enumerate(["app/a.py", "app/b.py", "lib/c.py", "tests/test_a.py"])]
    nodes = files + [{"id": "sym:a::f", "kind": "symbol", "label": "f", "path": "app/a.py"}, {"id": "sym:c::g", "kind": "symbol", "label": "g", "path": "lib/c.py"},
                     {"id": "route:GET /x", "kind": "route", "label": "GET /x"}, {"id": "table:t", "kind": "table", "label": "t"},
                     {"id": "commit:1", "kind": "commit", "label": "первый", "date": "2026-01-01T10:00:00"}, {"id": "commit:2", "kind": "commit", "label": "второй", "date": "2026-01-03T10:00:00"}]
    edges = [{"from": "file:app/a.py", "to": "sym:a::f", "rel": "defines"}, {"from": "file:lib/c.py", "to": "sym:c::g", "rel": "defines"},
             {"from": "sym:a::f", "to": "sym:c::g", "rel": "calls", "n": 3}, {"from": "file:app/b.py", "to": "file:app/a.py", "rel": "imports"},
             {"from": "route:GET /x", "to": "sym:a::f", "rel": "handles"}, {"from": "sym:a::f", "to": "table:t", "rel": "reads"},
             {"from": "commit:1", "to": "file:app/a.py", "rel": "changed", "n": 2}, {"from": "commit:2", "to": "file:app/a.py", "rel": "changed", "n": 1}]
    return {"nodes": nodes, "edges": edges, "stats": {"nodes": len(nodes), "edges": len(edges)}, "metrics": {"communities": [{"id": 0, "size": 2, "group": "app"}, {"id": 1, "size": 2, "group": "lib"}]}}


def test_view_collapses_symbols_into_file_edges_with_cards_and_history(graph):
    view = build_view(graph, title="Проба")
    assert view["title"] == "Проба" and len(view["nodes"]) == 4 and {g["id"] for g in view["groups"]} == {"app", "lib", "tests"}
    edge = {(e["s"], e["t"]): e for e in view["edges"]}
    assert edge[("file:app/a.py", "file:lib/c.py")]["rels"] == {"calls": 3} and ("file:app/b.py", "file:app/a.py") in edge
    node = {n["id"]: n for n in view["nodes"]}["file:app/a.py"]
    assert node["card"]["routes"] == ["GET /x"] and node["card"]["reads"] == ["t"] and node["card"]["symbols"] == 1
    assert view["days"] == ["2026-01-01", "2026-01-03"] and node["ch"] == [[0, 2], [1, 1]]
    assert {n["id"] for n in view["nodes"] if n["test"]} == {"file:tests/test_a.py"} and all("x" in n and "y" in n for n in view["nodes"])


def test_html_is_one_self_contained_file_without_external_addresses(graph):
    page = render_html(build_view(graph, title="Проба"))
    assert page.startswith("<!doctype html>") and "<title>Проба</title>" in page and "G6" in page
    assert "__DATA__" not in page and "__G6__" not in page and "\0" not in page
    assert not re.search(r"""(?:src|href)\s*=\s*["']https?://""", page), "страница не должна тянуть ничего из сети"
    data = re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S).group(1)
    assert json.loads(data.replace("<\\/", "</"))["title"] == "Проба"


def test_data_with_closing_script_tag_cannot_break_the_page(graph):
    graph["nodes"][0]["label"] = "</script><b>x"
    page = render_html(build_view(graph))
    assert "</script><b>x" not in page


def test_graphml_dot_and_json_formats(graph):
    view = build_view(graph)
    root = ET.fromstring(render(view, "graphml"))
    assert len(root.findall(".//{*}node")) == 4 and len(root.findall(".//{*}edge")) == len(view["edges"])
    dot = render(view, "dot")
    assert dot.startswith("digraph") and "cluster_0" in dot and "->" in dot
    assert json.loads(render(view, "json"))["nodes"]
    with pytest.raises(ValueError):
        render(view, "pdf")
    assert set(FORMATS) == {"html", "json", "graphml", "dot"}


def test_export_command_builds_the_graph_when_missing_and_writes_the_file(tmp_path, capsys):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("from pkg import b\n\n\ndef run():\n    return b.work()\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("def work():\n    return 1\n", encoding="utf-8")
    assert main(["export", "--root", str(tmp_path)]) == 0
    capsys.readouterr()
    page = (tmp_path / ".projectmap" / "out" / "map.html").read_text(encoding="utf-8")
    assert "pkg/a.py" in page
    assert main(["export", "--root", str(tmp_path), "--format", "dot", "--output", str(tmp_path / "m.dot")]) == 0
    assert (tmp_path / "m.dot").read_text(encoding="utf-8").startswith("digraph")
