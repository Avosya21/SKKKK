"""Проверка покрытия карты: роуты, таблицы БД и переменные окружения из кода должны быть упомянуты в PROJECT-MAP.md."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load_drift():
    spec = importlib.util.spec_from_file_location("check_map_drift", ROOT / "scripts" / "check_map_drift.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_map_drift"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_param_names_in_urls_are_normalised():
    drift = _load_drift()
    assert drift._norm_params("/projects/{project_id}/people/{person_id}") == "/projects/{}/people/{}"
    assert drift._norm_params("/projects/…/rebuild-tz") == "/projects/{}/rebuild-tz"


def test_system_and_smoke_env_vars_are_ignored():
    drift = _load_drift()
    for name in ("PATH", "LocalAppData", "ProgramFiles(x86)", "PYTEST_CURRENT_TEST", "SMOKE_BASE", "SMOKE_{}_PW"):
        assert drift.ENV_IGNORE.match(name), name
    for name in ("NEMOTRON_DIAR_MODEL", "JOB_MAX_ATTEMPTS", "LIGHTRAG_URL"):
        assert not drift.ENV_IGNORE.match(name), name


def test_coverage_sees_a_route_missing_from_the_map():
    drift = _load_drift()
    empty = drift.coverage("")
    assert "GET /admin/map" in empty["routes"]
    assert "artifacts" in empty["tables"] and "JWT_EXPIRE_MINUTES" in empty["env"]
    full = drift.coverage("/admin/map " + " ".join(empty["tables"]) + " " + " ".join(empty["env"])
                          + " " + " ".join(r.split(" ", 1)[1] for r in empty["routes"]))
    assert full == {"routes": [], "tables": [], "env": []}


def test_every_route_table_and_env_var_is_in_the_map():
    """Новый роут, таблица БД или переменная окружения без записи в PROJECT-MAP.md — падение теста.
    Лечение: описать в карте (§5, «Модели», §12.2); системные и внутренние переменные — в `ENV_IGNORE`."""
    drift = _load_drift()
    report = drift.analyse()
    assert not report["missing_routes"], report["missing_routes"][:10]
    assert not report["missing_tables"], report["missing_tables"][:10]
    assert not report["missing_env"], report["missing_env"][:10]
