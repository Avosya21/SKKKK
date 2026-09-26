from __future__ import annotations

import time

from app.app.services import build_info


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_build_status_fresh_and_stale(monkeypatch):
    monkeypatch.setattr(build_info, "STARTED_AT", 1000.0)
    monkeypatch.setattr(build_info, "worker_started_at", lambda: 1000.0)
    monkeypatch.setattr(build_info, "_mtimes", lambda: [(900.0, "app/app/old.py")])
    assert build_info.build_status()["stale"] is False
    monkeypatch.setattr(build_info, "_mtimes", lambda: [(2000.0, "app/app/services/tz.py"), (900.0, "app/app/old.py")])
    status = build_info.build_status()
    assert status["stale"] and status["api_stale"] and status["worker_stale"]
    assert status["api_changed"] == ["app/app/services/tz.py"]


def test_worker_stale_independent_of_api(monkeypatch):
    monkeypatch.setattr(build_info, "STARTED_AT", 3000.0)  # API стартовал позже правки
    monkeypatch.setattr(build_info, "worker_started_at", lambda: 1000.0)  # воркер — раньше
    monkeypatch.setattr(build_info, "_mtimes", lambda: [(2000.0, "app/app/config.py")])
    status = build_info.build_status()
    assert status["api_stale"] is False and status["worker_stale"] is True and status["stale"] is True


def test_watched_files_exclude_tests_and_static():
    names = {p.relative_to(build_info.ROOT).as_posix() for p in build_info._watched_files()}
    assert "app/app/main.py" in names
    assert not any("/tests/" in n or "/static/" in n or "__pycache__" in n for n in names)


def test_health_ready_build_hides_files_from_non_admin(client, admin_token, manager_token, monkeypatch):
    monkeypatch.setattr(build_info, "STARTED_AT", 1000.0)
    monkeypatch.setattr(build_info, "worker_started_at", lambda: None)
    monkeypatch.setattr(build_info, "_mtimes", lambda: [(time.time(), "app/app/secret_name.py")])
    adm = client.get("/health/ready", headers=_auth(admin_token)).json()["build"]
    mgr = client.get("/health/ready", headers=_auth(manager_token)).json()["build"]
    assert adm["stale"] and adm["api_changed"] == ["app/app/secret_name.py"]
    assert mgr == {"stale": True}
