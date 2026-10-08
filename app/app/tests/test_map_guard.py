"""Защита актуальности карты: проверка сообщения коммита, разбор расхождений, наличие git-хуков."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ROUTER = "app/app/routers/knowledge.py"
TEMPLATE = "app/app/templates/project.html"


def test_surface_change_without_map_or_note_is_rejected():
    msg = _load("check_map_commit_msg")
    assert msg.needs_note([ROUTER, "app/app/services/tz.py"], "Проекты: правка") == [ROUTER]
    assert msg.needs_note(["app/app/models/person.py", "app/app/config.py"], "x") == ["app/app/models/person.py", "app/app/config.py"]
    assert msg.needs_note([TEMPLATE], "x") == [TEMPLATE]
    assert msg.needs_note(["app/app/static/people.js"], "x") == ["app/app/static/people.js"]
    assert msg.needs_note([".env.example"], "x") == [".env.example"]


def test_map_in_commit_or_explicit_note_passes():
    msg = _load("check_map_commit_msg")
    assert msg.needs_note([ROUTER, "docs/PROJECT-MAP.md"], "x") == []
    assert msg.needs_note([ROUTER, "docs/project-map/manifest.yaml"], "x") == []
    assert msg.needs_note([ROUTER], "Правка\n\nКарта: не требуется — опечатка в тексте ошибки") == []
    assert msg.needs_note([ROUTER], "Правка\nкарта: НЕ ТРЕБУЕТСЯ - стиль кода") == []


def test_note_without_reason_does_not_count():
    msg = _load("check_map_commit_msg")
    assert msg.needs_note([ROUTER], "Правка\nКарта: не требуется") == [ROUTER]
    assert msg.needs_note([ROUTER], "Правка\nКарта: не требуется — ") == [ROUTER]
    assert msg.needs_note([ROUTER], "Правка\n# Карта: не требуется — закомментировано") == [ROUTER]


def test_tests_docs_scripts_merges_and_reverts_need_no_note():
    msg = _load("check_map_commit_msg")
    assert msg.needs_note(["app/app/tests/test_x.py", "docs/PLAN.md", "scripts/qa_pages.py", "app/app/services/tz.py"], "x") == []
    assert msg.needs_note(["app/app/static/vendor/bootstrap/bootstrap.min.css"], "x") == []
    assert msg.needs_note([ROUTER], "Merge branch 'main'") == []
    assert msg.needs_note([ROUTER], 'Revert "Проекты: правка"') == []


def test_staged_check_only_runs_for_files_that_affect_the_map():
    staged = _load("check_map_staged")
    for path in ("app/app/routers/x.py", "scripts/a.py", "asr/b.py", "docs/PROJECT-MAP.md", "docs/project-map/manifest.yaml", ".env.example"):
        assert staged.is_relevant(path), path
    for path in ("docs/PLAN.md", "README.md", "data/x.db", "logo_drafts/a.svg"):
        assert not staged.is_relevant(path), path


def test_staged_report_formats_every_kind_of_drift():
    staged = _load("check_map_staged")
    report = {
        "missing_in_map": ["app/app/services/new.py"],
        "dead_nodes": [["svc_gone", "app/app/services/definitely_gone.py"]],
        "bad_edges": [{"from": "a", "to": "b", "relation": "calls"}],
        "registry_stale": True,
        "missing_routes": ["GET /zz"],
        "missing_tables": ["zz_table"],
        "missing_env": ["ZZ_VAR"],
    }
    text = "\n".join(staged.format_problems(report))
    for needle in ("new.py", "svc_gone", "a -> b", "GET /zz", "zz_table", "ZZ_VAR", "реестр модулей"):
        assert needle in text, needle
    assert staged.format_problems({**report, "missing_in_map": [], "dead_nodes": [], "bad_edges": [], "registry_stale": False,
                                   "missing_routes": [], "missing_tables": [], "missing_env": []}) == []


def test_node_for_a_file_outside_git_is_not_dead():
    staged = _load("check_map_staged")
    report = {"missing_in_map": [], "dead_nodes": [["file_login_allowlist", "Login_up.txt"]], "bad_edges": [], "registry_stale": False,
              "missing_routes": [], "missing_tables": [], "missing_env": []}
    # файл вне git (или игнорируемый) в снимок индекса не попадает, но мёртвым узел не считается
    if (ROOT / "Login_up.txt").exists():
        assert staged.format_problems(report) == []


def test_staged_python_that_does_not_parse_is_reported(monkeypatch):
    staged = _load("check_map_staged")
    files = {"ok.py": b"x = 1\n", "bad.py": b"def f(:\n    pass\n", "notes.md": b"("}
    monkeypatch.setattr(staged, "_git", lambda *args, **kw: files[args[1][1:]])
    found = staged.syntax_errors(["ok.py", "bad.py", "notes.md"])
    assert len(found) == 1 and found[0].startswith("bad.py:1:")


def test_installer_declares_both_map_hooks_and_the_scripts_exist():
    text = (ROOT / "scripts" / "install_hooks.ps1").read_text(encoding="utf-8-sig")
    for needle in ("pre-commit", "commit-msg", "SKKKK-MAP", "SKKKK-PRIVACY", "check_map_staged.py", "check_map_commit_msg.py",
                   "scan_personal_data.py"):
        assert needle in text, needle
    assert (ROOT / "scripts" / "check_map_staged.py").is_file() and (ROOT / "scripts" / "check_map_commit_msg.py").is_file()


def test_restart_and_bootstrap_install_the_hooks():
    for name in ("restart.ps1", "bootstrap.ps1"):
        assert "install_hooks.ps1" in (ROOT / "scripts" / name).read_text(encoding="utf-8-sig"), name


def test_git_hooks_that_guard_the_map_are_installed():
    """Хуки лежат вне git: в новой копии репозитория их нужно поставить (`scripts/install_hooks.ps1`; `INSTALL.bat` и
    `restart.ps1` делают это сами). Без них карту ничто не защищает."""
    doctor = _load("index_doctor")
    missing = doctor.hooks_missing()
    if missing is None:
        pytest.skip("не git-копия: каталога хуков нет")
    assert missing == [], f"не установлены хуки {missing}: powershell -ExecutionPolicy Bypass -File scripts\\install_hooks.ps1"


def test_privacy_scan_reads_the_index_version_and_skips_when_nothing_to_check():
    scan = _load("scan_personal_data")
    src = (ROOT / "scripts" / "scan_personal_data.py").read_text(encoding="utf-8")
    assert "def _lines" in src and 'f":{rel}"' in src  # с --staged читается blob из индекса, не рабочая копия
    assert "if args.staged and not files" in src  # коммит без текстовых файлов базу не открывает
    assert "базы портала нет" in src and "настройки портала не прочитаны" in src  # чистая копия коммит не блокирует
    assert callable(scan.main)
