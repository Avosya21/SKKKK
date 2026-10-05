"""Командная строка projectmap: версия, список команд, построение графа."""

from __future__ import annotations

from projectmap import __version__
from projectmap.cli import COMMANDS, build_parser, main


def test_version_command_prints_the_package_version(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"projectmap {__version__}"


def test_every_command_is_listed_in_help_and_nothing_is_left_unimplemented():
    text = build_parser().format_help()
    for command in ["version", "init", "index", "graph", "check", "export", "serve", "hooks", "mcp"]:
        assert command in text
    assert not COMMANDS, "объявленных, но не реализованных команд не осталось"


def test_index_builds_graph_with_metrics_and_graph_adds_atlas(tmp_path, capsys):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("from pkg import b\n\n\ndef run():\n    return b.work()\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("def work():\n    return 1\n", encoding="utf-8")
    (tmp_path / ".projectmap").mkdir()
    (tmp_path / ".projectmap" / "config.yaml").write_text("graph: .projectmap/out/graph.json\n", encoding="utf-8")
    assert main(["index", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "узлов:" in out and "записано:" in out
    graph_file = tmp_path / ".projectmap" / "out" / "graph.json"
    assert graph_file.is_file() and not (tmp_path / ".projectmap" / "out" / "atlas.md").exists()
    assert main(["graph", "--root", str(tmp_path), "--no-metrics"]) == 0
    capsys.readouterr()
    atlas = (tmp_path / ".projectmap" / "out" / "atlas.md").read_text(encoding="utf-8")
    assert atlas.startswith("# Атлас графа проекта") and "Из чего состоит граф" in atlas
    assert main(["check", "--root", str(tmp_path), "--only", "files"]) == 0
