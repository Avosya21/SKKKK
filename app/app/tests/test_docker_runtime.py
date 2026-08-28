from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.app.services import docker_runtime as dr


def test_docker_daemon_running_true(monkeypatch):
    monkeypatch.setattr(dr.subprocess, "run", lambda *a, **k: MagicMock(returncode=0))
    assert dr.docker_daemon_running() is True


def test_docker_daemon_running_false(monkeypatch):
    monkeypatch.setattr(dr.subprocess, "run", lambda *a, **k: MagicMock(returncode=1))
    assert dr.docker_daemon_running() is False


def test_ensure_docker_daemon_already_up(monkeypatch):
    calls: list[str] = []

    def fake_start():
        calls.append("start")

    monkeypatch.setattr(dr, "docker_daemon_running", lambda: True)
    monkeypatch.setattr(dr, "start_docker_desktop", fake_start)
    dr.ensure_docker_daemon(timeout=5)
    assert calls == []


def test_ensure_docker_daemon_starts_and_waits(monkeypatch):
    state = {"ready": False}

    def running():
        return state["ready"]

    def fake_start():
        state["ready"] = True

    monkeypatch.setattr(dr, "docker_daemon_running", running)
    monkeypatch.setattr(dr, "start_docker_desktop", fake_start)
    dr.ensure_docker_daemon(timeout=5)


def test_ensure_asr_containers_compose_when_missing(monkeypatch, tmp_path):
    compose = tmp_path / "asr" / "docker-compose.yml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(dr, "COMPOSE_FILE", compose)
    monkeypatch.setattr(dr, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dr, "ensure_docker_daemon", lambda **k: None)

    exists = set()

    def fake_exists(name: str) -> bool:
        return name in exists

    def fake_compose_up():
        exists.update(dr.ASR_CONTAINERS)

    runs: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        runs.append(list(cmd))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(dr, "_container_exists", fake_exists)
    monkeypatch.setattr(dr, "_compose_up", fake_compose_up)
    monkeypatch.setattr(dr.subprocess, "run", fake_run)

    dr.ensure_asr_containers(restart=True)

    assert runs == [["docker", "restart", *dr.ASR_CONTAINERS]]


def test_start_docker_desktop_via_exe(monkeypatch, tmp_path):
    exe = tmp_path / "Docker Desktop.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(dr, "_docker_desktop_paths", lambda: [exe])

    cli_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        cli_calls.append(list(cmd))
        return MagicMock(returncode=1, stdout="", stderr="")

    launched: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        launched.append(list(cmd))
        return MagicMock()

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    monkeypatch.setattr(dr.subprocess, "Popen", fake_popen)

    dr.start_docker_desktop()

    assert cli_calls[0][:3] == ["docker", "desktop", "start"]
    assert launched == [[str(exe)]]


def test_start_docker_desktop_missing_raises(monkeypatch):
    monkeypatch.setattr(dr, "_docker_desktop_paths", lambda: [])
    monkeypatch.setattr(
        dr.subprocess,
        "run",
        lambda *a, **k: MagicMock(returncode=1, stdout="", stderr=""),
    )
    with pytest.raises(RuntimeError, match="Docker Desktop не найден"):
        dr.start_docker_desktop()


def test_asr_containers_prod_is_gigaam_only():
    assert dr.ASR_CONTAINERS == ("skkkk-gigaam",)


def test_compose_up_starts_gigaam_service(monkeypatch, tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  gigaam: {}\n", encoding="utf-8")
    monkeypatch.setattr(dr, "COMPOSE_FILE", compose)
    monkeypatch.setattr(dr, "ENV_FILE", tmp_path / "missing.env")
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    dr._compose_up()
    assert captured
    assert captured[0][-2:] == ["-d", "gigaam"]
    assert "whisper" not in captured[0]
    assert "tone" not in captured[0]
