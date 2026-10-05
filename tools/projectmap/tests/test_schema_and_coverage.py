"""Схема графа и модель охвата: слои, проверка целостности, расчёт доли и отчёты."""

from __future__ import annotations

from projectmap.config import glob_to_regex, load_config, parse_config
from projectmap.coverage import Coverage, Report, make_coverage, render_markdown, render_text
from projectmap.schema import (LAYER_IDS, NODE_KINDS, RELATIONS, count_by_layer, layer_of_kind, layer_of_relation, validate_graph)


def _graph(**extra):
    graph = {
        "nodes": [{"id": "f:a", "kind": "file", "path": "a.py"}, {"id": "s:a.f", "kind": "symbol", "path": "a.py"},
                  {"id": "r:get", "kind": "route"}],
        "edges": [{"from": "f:a", "to": "s:a.f", "rel": "defines"}],
    }
    graph.update(extra)
    return graph


def test_every_kind_and_relation_belongs_to_a_known_layer():
    assert {layer for layer, _ in NODE_KINDS.values()} <= set(LAYER_IDS)
    assert {layer for layer, _ in RELATIONS.values()} <= set(LAYER_IDS)
    assert layer_of_relation("m:subprocess") == "meaning"
    assert layer_of_kind("route") == "interfaces" and layer_of_kind("nope") is None


def test_valid_graph_has_no_problems():
    assert validate_graph(_graph()) == []


def test_validation_finds_duplicates_dangling_edges_and_unknown_kinds():
    graph = _graph()
    graph["nodes"] += [{"id": "f:a", "kind": "file"}, {"id": "x", "kind": "weird"}, {"kind": "file"}]
    graph["edges"] += [{"from": "f:a", "to": "missing", "rel": "calls"}, {"from": "f:a", "to": "s:a.f", "rel": "strange"}, {"from": "f:a"}]
    text = "\n".join(f"{p.severity}: {p.message}" for p in validate_graph(graph))
    for needle in ("повторяется id узла: f:a", "несуществующие узлы: 1", "вид узла вне схемы: weird", "вид связи вне схемы: strange",
                   "нет полей id", "нет полей to, rel"):
        assert needle in text, needle


def test_graph_without_lists_is_an_error():
    assert validate_graph({"nodes": []})[0].severity == "error"


def test_count_by_layer():
    counts = count_by_layer(_graph())
    assert counts["structure"] == {"nodes": 2, "edges": 1}
    assert counts["interfaces"]["nodes"] == 1


def test_coverage_ratio_and_status():
    assert Coverage("structure", "p", "t", 0, 0).status == "n/a" and Coverage("structure", "p", "t", 0, 0).ratio is None
    assert Coverage("structure", "p", "t", 4, 3).ratio == 0.75
    assert Coverage("structure", "p", "t", 4, 3, target=0.9).status == "gap"
    assert Coverage("structure", "p", "t", 4, 3, target=0.7).status == "ok"
    assert Coverage("structure", "p", "t", 4, 3).status == "ok"  # цели нет — не пробел


def test_make_coverage_reports_missing_items_sorted_and_deduplicated():
    c = make_coverage("structure", "files", "Файлы", ["b", "a", "a", "c"], {"c"})
    assert (c.total, c.covered, c.missing) == (3, 1, ["a", "b"])


def test_report_layer_ratio_and_gaps():
    report = Report([Coverage("structure", "a", "A", 10, 5, target=0.9), Coverage("structure", "b", "B", 10, 10),
                     Coverage("data", "c", "C", 0, 0)])
    assert report.layer_ratio("structure") == 0.75 and report.layer_ratio("data") is None and report.layer_ratio("tests") is None
    assert [c.probe for c in report.gaps()] == ["a"]
    assert report.to_dict()["gaps"] == 1


def test_renderers_show_layers_gaps_and_missing_examples():
    report = Report([make_coverage("structure", "files", "Файлы в графе", ["a.py", "b.py"], {"a.py"})])
    report.coverages[0].target = 0.9
    text, md = render_text(report), render_markdown(report)
    assert "Структура: 50%" in text and "✗ Файлы в графе: 1/2" in text and "Ниже цели: 1" in text
    assert "| Структура | 50% | 1 |" in md and "`b.py`" in md and "Охват" in md


def test_glob_masks():
    assert glob_to_regex("dir/**").match("dir/a/b.py") and not glob_to_regex("dir/**").match("other/a.py")
    assert glob_to_regex("**/*.png").match("a/b/c.png") and glob_to_regex("**/*.png").match("c.png")
    assert glob_to_regex("*.md").match("a.md") and not glob_to_regex("*.md").match("d/a.md")
    assert glob_to_regex("a?c").match("abc") and not glob_to_regex("a?c").match("a/c")


def test_config_defaults_and_loading(tmp_path):
    assert load_config(tmp_path).ignore == []
    (tmp_path / ".projectmap").mkdir()
    (tmp_path / ".projectmap" / "config.yaml").write_text("graph: g.json\nignore: ['x/**']\ntargets: {files: 0.9}\n", encoding="utf-8")
    config = load_config(tmp_path)
    assert config.graph == "g.json" and config.is_ignored("x/a.py") and not config.is_ignored("y/a.py") and config.targets == {"files": 0.9}
    assert parse_config(None).targets == {}
