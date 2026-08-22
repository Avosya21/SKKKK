import os
import subprocess
import sys
import time

import pytest

from app.app import main as main_module
from app.app.preflight import FAIL, OK, CheckResult

CHILD_TREE = (
    "import subprocess, sys, time;"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
    "print(p.pid, flush=True);"
    "time.sleep(120)"
)


def _alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
    ).stdout
    return str(pid) in out


@pytest.mark.skipif(os.name != "nt", reason="ловушка с обёрткой venv специфична для Windows")
def test_stop_worker_kills_the_whole_process_tree():
    """venv\\Scripts\\python.exe — обёртка: убить надо и её потомка.

    Иначе воркер продолжает разбирать очередь после остановки портала.
    """
    parent = subprocess.Popen(
        [sys.executable, "-c", CHILD_TREE],
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pid = int(parent.stdout.readline().strip())
    assert _alive(child_pid)

    main_module._stop_worker(parent)

    deadline = time.time() + 15
    while time.time() < deadline and _alive(child_pid):
        time.sleep(0.3)

    assert parent.poll() is not None
    assert not _alive(child_pid), "потомок пережил остановку воркера"


def test_all_mode_does_not_start_anything_when_a_check_fails(monkeypatch):
    spawned: list[str] = []
    monkeypatch.setattr(main_module, "_preflight", lambda *a, **k: 2)
    monkeypatch.setattr(main_module, "_spawn_worker", lambda: spawned.append("worker"))
    monkeypatch.setattr(main_module, "_serve", lambda *a, **k: spawned.append("api"))

    assert main_module.main(["all"]) == 2
    assert spawned == []


def test_all_mode_starts_worker_and_api(monkeypatch):
    started: list[str] = []

    class _FakeProcess:
        pid = 4242

    monkeypatch.setattr(main_module, "_preflight", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "_spawn_worker", lambda: (started.append("worker"), _FakeProcess())[1])
    monkeypatch.setattr(main_module, "_serve", lambda *a, **k: started.append("api"))
    monkeypatch.setattr(main_module, "_stop_worker", lambda p: started.append("stop"))

    assert main_module.main(["all"]) == 0
    assert started == ["worker", "api", "stop"]


def test_all_mode_is_noop_when_server_already_running(monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(main_module, "_preflight", lambda *a, **k: 1)
    monkeypatch.setattr(main_module, "_spawn_worker", lambda: started.append("worker"))
    monkeypatch.setattr(main_module, "_serve", lambda *a, **k: started.append("api"))
    assert main_module.main(["all"]) == 0
    assert started == []


def test_worker_is_stopped_even_if_api_crashes(monkeypatch):
    stopped: list[int] = []

    class _FakeProcess:
        pid = 77

    def _boom(*_a, **_k):
        raise RuntimeError("uvicorn упал")

    monkeypatch.setattr(main_module, "_preflight", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "_spawn_worker", _FakeProcess)
    monkeypatch.setattr(main_module, "_serve", _boom)
    monkeypatch.setattr(main_module, "_stop_worker", lambda p: stopped.append(p.pid))

    with pytest.raises(RuntimeError):
        main_module.main(["all"])
    assert stopped == [77]


def test_check_mode_returns_two_on_failure(monkeypatch):
    monkeypatch.setattr(
        "app.app.preflight.run_preflight",
        lambda *a, **k: [CheckResult("database", FAIL, "нет базы")],
    )
    assert main_module.main(["check"]) == 2


def test_check_mode_returns_zero_when_everything_passes(monkeypatch):
    monkeypatch.setattr(
        "app.app.preflight.run_preflight",
        lambda *a, **k: [CheckResult("database", OK, "ок")],
    )
    assert main_module.main(["check"]) == 0


def test_default_mode_is_all(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(main_module, "_run_all", lambda *a, **k: calls.append("all") or 0)
    assert main_module.main([]) == 0
    assert calls == ["all"]


def test_skip_checks_bypasses_preflight(monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr("app.app.preflight.run_preflight", lambda *a, **k: ran.append("checked") or [])
    monkeypatch.setattr(main_module, "_spawn_worker", lambda: None)
    monkeypatch.setattr(main_module, "_serve", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "_stop_worker", lambda p: None)

    assert main_module.main(["all", "--skip-checks"]) == 0
    assert ran == []
