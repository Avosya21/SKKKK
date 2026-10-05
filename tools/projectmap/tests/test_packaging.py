"""Упаковка: `init` определяет стек и пишет конфиг, git-хуки работают на настоящем репозитории, MCP отвечает по протоколу."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from projectmap import hooks, mcp_server
from projectmap.cli import build_and_write, main
from projectmap.config import load_config
from projectmap.detect import detect_stack
from projectmap.init import ensure_gitignore, render_config

PKG_PARENT = str(Path(__file__).resolve().parents[1])


def _project(root: Path) -> Path:
    files = {
        "app/main.py": "from fastapi import FastAPI\nfrom app import util\n\napp = FastAPI()\n\n\n@app.get('/ping')\ndef ping():\n    return util.pong()\n",
        "app/util.py": "def pong():\n    return 'pong'\n\n\ndef helper():\n    return pong()\n",
        "tests/test_util.py": "from app import util\n\n\ndef test_pong():\n    assert util.pong() == 'pong'\n",
        "docs/PROJECT-MAP.md": "# Карта\nСм. `app/main.py`.\n",
        "node_modules/x/index.js": "module.exports = 1;\n",
        "logo.png": "not really a png",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": PKG_PARENT, "PYTHONUTF8": "1"}
    result = subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                            capture_output=True, text=True, encoding="utf-8", env=env)
    if check and result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return result


@pytest.fixture()
def repo(tmp_path):
    root = _project(tmp_path / "proj")
    _git(root, "init", "-q")
    return root


def test_detect_stack_finds_languages_frameworks_tests_and_noise(repo):
    _git(repo, "add", "-A")
    stack = detect_stack(repo)
    assert stack["languages"]["python"] == 3 and "FastAPI / Starlette" in stack["frameworks"] and stack["tests"] == 1
    masks = [m for m, _ in stack["ignore"]]
    assert "**/node_modules/**" in masks and "**/*.png" in masks and stack["map_files"] == ["docs/PROJECT-MAP.md"]


def test_init_writes_a_valid_config_gitignore_and_refuses_to_overwrite(repo, capsys):
    _git(repo, "add", "-A")
    assert main(["init", "--root", str(repo)]) == 0
    config = load_config(repo)
    assert config.title == "proj" and "**/node_modules/**" in config.ignore and config.map_files == ["docs/PROJECT-MAP.md"]
    assert yaml.safe_load((repo / ".projectmap" / "config.yaml").read_text(encoding="utf-8"))["adapters"] == "auto"
    ignore = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".projectmap/out/" in ignore and ".projectmap/private-terms.txt" in ignore
    assert (repo / ".projectmap" / "private-terms.txt").is_file()
    capsys.readouterr()
    assert main(["init", "--root", str(repo)]) == 1 and "уже есть" in capsys.readouterr().out
    assert ensure_gitignore(repo) == [], "повторный init не дублирует правила"


def test_three_commands_give_a_graph_a_check_and_a_viewer(repo, capsys):
    _git(repo, "add", "-A")
    assert main(["init", "--root", str(repo)]) == 0
    assert main(["index", "--root", str(repo)]) == 0
    assert main(["check", "--root", str(repo)]) == 0
    assert main(["export", "--root", str(repo)]) == 0
    assert "записано:" in capsys.readouterr().out and (repo / ".projectmap" / "out" / "map.html").is_file()
    graph = json.loads((repo / ".projectmap" / "out" / "graph.json").read_text(encoding="utf-8"))
    assert any(n["kind"] == "route" and n["label"] == "GET /ping" for n in graph["nodes"])


def test_render_config_is_valid_yaml_for_a_project_without_anything():
    data = yaml.safe_load(render_config(Path("empty"), {"ignore": [], "map_files": [], "languages": {}}))
    assert data["ignore"] == [] and data["map"]["files"] == []


def test_hooks_install_status_uninstall_and_a_foreign_hook_is_kept(repo):
    directory = hooks.hooks_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pre-commit").write_text("#!/bin/sh\necho foreign\n", encoding="utf-8", newline="\n")
    assert hooks.status(repo) == {"pre-commit": "foreign", "commit-msg": "absent"}
    result = hooks.install(repo)
    assert result["pre-commit"].startswith("skipped") and result["commit-msg"] == "installed"
    assert hooks.install(repo, force=True)["pre-commit"] == "installed"
    assert (directory / "pre-commit.projectmap-backup").is_file() and hooks.status(repo) == {"pre-commit": "ours", "commit-msg": "ours"}
    assert "возвращён" in hooks.uninstall(repo)["pre-commit"]
    assert "foreign" in (directory / "pre-commit").read_text(encoding="utf-8") and hooks.status(repo)["commit-msg"] == "absent"


def _commit(repo: Path, message: str) -> subprocess.CompletedProcess[str]:
    return _git(repo, "commit", "-m", message, check=False)


def test_pre_commit_blocks_private_words_and_commit_msg_requires_the_map(repo):
    assert main(["init", "--root", str(repo)]) == 0
    (repo / ".projectmap" / "private-terms.txt").write_text("# слова\nИванов Пётр\n", encoding="utf-8")
    hooks.install(repo)
    _git(repo, "add", "-A")
    first = _commit(repo, "Старт: первый коммит с картой")
    assert first.returncode == 0, first.stderr  # карта в коммите, личных слов нет

    (repo / "app" / "util.py").write_text("def pong():\n    return 'pong'  # автор Иванов Пётр\n", encoding="utf-8")
    _git(repo, "add", "-A")
    blocked = _commit(repo, "Правка: комментарий")
    assert blocked.returncode != 0 and "личные данные" in blocked.stderr and "app/util.py:2" in blocked.stderr
    assert "Иванов" not in blocked.stderr, "само слово в вывод не попадает"

    (repo / "app" / "util.py").write_text("def pong():\n    return 'pong!'\n", encoding="utf-8")
    _git(repo, "add", "-A")
    no_map = _commit(repo, "Правка: код без карты")
    assert no_map.returncode != 0 and "карты в коммите нет" in no_map.stderr
    waived = _commit(repo, "Правка: восклицание\n\nКарта: не требуется — поведение не меняется")
    assert waived.returncode == 0, waived.stderr

    (repo / "app" / "util.py").write_text("def pong():\n    return 'pong?'\n", encoding="utf-8")
    (repo / "docs" / "PROJECT-MAP.md").write_text("# Карта\nСм. `app/main.py` и `app/util.py`.\n", encoding="utf-8")
    _git(repo, "add", "-A")
    assert _commit(repo, "Правка: вопрос вместе с картой").returncode == 0


def test_waiver_needs_a_reason():
    assert hooks.waiver_reason("x\nКарта: не требуется — опечатка", "Карта: не требуется") == "опечатка"
    assert hooks.waiver_reason("Карта: не требуется", "Карта: не требуется") is None
    assert hooks.waiver_reason("без отказа", "Карта: не требуется") is None


@pytest.fixture()
def server(repo):
    graph_path = repo / ".projectmap" / "out" / "graph.json"
    assert main(["init", "--root", str(repo)]) == 0
    build_and_write(repo, load_config(repo), graph_path)
    return mcp_server.Graph(repo, graph_path)


def _call(server, name, **arguments):
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, server)
    assert reply["result"]["isError"] is False, reply
    return reply["result"]["content"][0]["text"]


def test_mcp_protocol_lists_tools_and_answers_ping(server):
    init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, server)
    assert init["result"]["serverInfo"]["name"] == "projectmap"
    names = {t["name"] for t in mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, server)["result"]["tools"]}
    assert names == {"search", "symbol", "file", "callers", "callees", "neighbors", "impact", "stats", "coverage"}
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, server) is None
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"}, server)["error"]["code"] == -32601
    bad = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}}, server)
    assert bad["result"]["isError"] is True


def test_mcp_tools_answer_from_the_graph(server):
    assert "app/util.py" in _call(server, "search", query="util", kind="file")
    assert "app/util.py:1" in _call(server, "symbol", query="pong")
    card = _call(server, "file", query="util.py")
    assert "app/main.py" in card and "тесты: tests/test_util.py" in card
    assert "helper" in _call(server, "callers", query="pong")
    assert "pong" in _call(server, "callees", query="helper")
    impact = _call(server, "impact", query="app/util.py")
    assert "app/main.py" in impact and "test_util" in impact
    assert "ping" in _call(server, "neighbors", query="pong", direction="in")
    assert "узлов" in _call(server, "stats") and "Структура" in _call(server, "coverage")


def test_mcp_client_config_points_at_this_python(repo):
    config = mcp_server.client_config(repo)["mcpServers"]["projectmap"]
    assert config["args"][:3] == ["-m", "projectmap", "mcp"] and Path(config["command"]).name.lower().startswith("python")


def test_mcp_works_over_stdio_like_a_real_client(repo):
    assert main(["init", "--root", str(repo)]) == 0
    requests = "\n".join(json.dumps(m) for m in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search", "arguments": {"query": "pong"}}})) + "\n"
    env = {**os.environ, "PYTHONPATH": PKG_PARENT, "PYTHONUTF8": "1"}
    done = subprocess.run([sys.executable, "-m", "projectmap", "mcp", "--root", str(repo)], input=requests, capture_output=True,
                          text=True, encoding="utf-8", env=env, timeout=120)
    lines = [json.loads(line) for line in done.stdout.splitlines()]
    assert [m["id"] for m in lines] == [1, 2] and "pong" in lines[1]["result"]["content"][0]["text"]
