"""Пробы охвата на небольшом проекте во временной папке и команда `projectmap check`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from projectmap.cli import main
from projectmap.config import Config
from projectmap.context import ProjectContext
from projectmap.probes import is_test_path, run_probes


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def project(tmp_path):
    _write(tmp_path, "src/app.py", "def run():\n    pass\n")
    _write(tmp_path, "src/helper_tool.py", "def help_me():\n    pass\n")
    _write(tmp_path, "src/other.py", "def other():\n    pass\n")
    _write(tmp_path, "tests/test_app.py", "def test_run():\n    pass\n")
    _write(tmp_path, "tools/deploy.ps1", "function Start-Deploy {\n}\nfunction Stop-Deploy {\n}\n")
    _write(tmp_path, "docker-compose.yml", "services:\n  web:\n    image: x\n  db:\n    image: y\n")
    _write(tmp_path, "docs/README.md", "См. `src/app.py`, `src/gone.py` и `tools/deploy.ps1`.\n")
    _write(tmp_path, "src/caller.py", "import subprocess\nsubprocess.run(['python', 'helper_tool.py'])\n")
    _write(tmp_path, "data/big.csv", "a,b\n")
    graph = {
        "nodes": [
            {"id": "f:app", "kind": "file", "path": "src/app.py"}, {"id": "s:run", "kind": "symbol", "path": "src/app.py", "label": "run"},
            {"id": "f:helper", "kind": "file", "path": "src/helper_tool.py"},
            {"id": "s:help", "kind": "symbol", "path": "src/helper_tool.py", "label": "help_me"},
            {"id": "f:caller", "kind": "file", "path": "src/caller.py"},
            {"id": "f:test", "kind": "file", "path": "tests/test_app.py"}, {"id": "s:test", "kind": "symbol", "path": "tests/test_app.py"},
            {"id": "f:deploy", "kind": "file", "path": "tools/deploy.ps1"},
            {"id": "sym:deploy", "kind": "symbol", "path": "tools/deploy.ps1", "label": "Start-Deploy"},
            {"id": "svc:web", "kind": "service", "path": "docker-compose.yml", "label": "web"},
            {"id": "ui:btn", "kind": "ui", "sub": "button"}, {"id": "ui:lonely", "kind": "ui", "sub": "button"}, {"id": "ui:box", "kind": "ui", "sub": "div"}, {"id": "api:post", "kind": "route"},
            {"id": "tbl:users", "kind": "table"}, {"id": "tbl:log", "kind": "table"},
            {"id": "env:KEY", "kind": "env", "declared": True}, {"id": "env:OTHER", "kind": "env", "declared": True},
        ],
        "edges": [
            {"from": "f:app", "to": "s:run", "rel": "defines"}, {"from": "f:helper", "to": "s:help", "rel": "defines"},
            {"from": "f:test", "to": "s:test", "rel": "defines"}, {"from": "s:test", "to": "s:run", "rel": "covers"},
            {"from": "f:caller", "to": "f:helper", "rel": "runs"},
            {"from": "ui:btn", "to": "api:post", "rel": "calls_api"},
            {"from": "api:post", "to": "tbl:users", "rel": "writes"},
            {"from": "s:run", "to": "env:KEY", "rel": "reads_env"},
        ],
    }
    config = Config(ignore=["**/*.csv"], targets={"files": 0.9, "tests": 0.9})
    return ProjectContext(tmp_path, graph, config)


def _by_probe(report):
    return {c.probe: c for c in report.coverages}


def test_files_probe_counts_missing_files_and_reports_exclusions(project):
    c = _by_probe(run_probes(project, ["files"]))["files"]
    assert "data/big.csv" not in c.missing and "src/other.py" in c.missing and "docs/README.md" in c.missing
    assert c.covered == c.total - len(c.missing) and "исключено" in c.note and c.status == "gap"


def test_shell_functions_and_compose_services(project):
    probes = _by_probe(run_probes(project, ["shell_functions", "compose_services"]))
    assert (probes["shell_functions"].total, probes["shell_functions"].covered) == (2, 1)
    assert probes["shell_functions"].missing == ["tools/deploy.ps1::Stop-Deploy"]
    assert (probes["compose_services"].total, probes["compose_services"].covered) == (2, 1)
    assert probes["compose_services"].missing == ["docker-compose.yml::db"]


def test_script_refs_find_mentions_and_check_the_link(project):
    c = _by_probe(run_probes(project, ["script_refs"]))["script_refs"]
    assert c.total == 1 and c.covered == 1  # caller.py -> helper_tool.py есть как связь `runs`


def test_doc_refs_find_broken_links_and_missing_edges(project):
    probes = _by_probe(run_probes(project, ["doc_refs"]))
    valid, in_graph = probes["doc_refs_valid"], probes["doc_refs_in_graph"]
    assert (valid.total, valid.covered) == (3, 2) and valid.missing == ["docs/README.md → src/gone.py"]
    assert (in_graph.total, in_graph.covered) == (2, 0)


def test_interface_data_settings_and_test_probes(project):
    probes = _by_probe(run_probes(project, ["ui_api", "data_access", "env", "tests"]))
    assert (probes["ui_api"].total, probes["ui_api"].covered) == (2, 1)  # контейнер div в проверку не входит
    assert (probes["data_access"].total, probes["data_access"].covered) == (2, 1)
    assert (probes["env"].total, probes["env"].covered) == (1, 1)  # KEY читается и описана
    assert (probes["env_used"].total, probes["env_used"].covered) == (2, 1) and probes["env_used"].missing == ["env:OTHER"]
    tests = probes["tests"]
    assert tests.missing == ["src/helper_tool.py"] and tests.covered == 1 and tests.status == "gap"


def test_metrics_probe_is_zero_until_files_carry_metrics(project):
    assert _by_probe(run_probes(project, ["metrics"]))["metrics"].covered == 0
    project.nodes_of("file")[0]["community"] = 3
    assert _by_probe(run_probes(project, ["metrics"]))["metrics"].covered == 1


def test_test_path_detection():
    assert is_test_path("tests/a.py") and is_test_path("src/test_a.py") and is_test_path("a_test.py") and is_test_path("web/a.test.ts")
    assert not is_test_path("src/latest.py") and not is_test_path("src/contest/a.py")


def test_check_command_prints_report_and_strict_fails_on_gaps(project, capsys):
    root = project.root
    (root / "graph.json").write_text(json.dumps(project.graph), encoding="utf-8")
    (root / ".projectmap").mkdir()
    (root / ".projectmap" / "config.yaml").write_text("graph: graph.json\nignore: ['**/*.csv']\ntargets: {files: 0.99}\n", encoding="utf-8")
    assert main(["check", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Структура" in out and "Ниже цели: 1" in out
    assert main(["check", "--root", str(root), "--strict"]) == 1
    capsys.readouterr()
    assert main(["check", "--root", str(root), "--format", "json", "--only", "env"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [c["probe"] for c in data["coverages"]] == ["env", "env_used"]
    out_file = root / "report.md"
    assert main(["check", "--root", str(root), "--format", "markdown", "--output", str(out_file)]) == 0
    assert "| Слой | Охват |" in out_file.read_text(encoding="utf-8")


def test_check_reports_missing_graph(tmp_path, capsys):
    assert main(["check", "--root", str(tmp_path)]) == 2
    assert "графа нет" in capsys.readouterr().err
