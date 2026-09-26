from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.app.config import Settings
from app.app.services import ollama_runtime as orr


def _settings() -> Settings:
    return Settings(ollama_base_url="http://127.0.0.1:11434")


def test_ollama_api_reachable_true(monkeypatch):
    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return FakeResp()

    monkeypatch.setattr(orr.httpx, "Client", FakeClient)
    assert orr.ollama_api_reachable(_settings()) is True


def test_ensure_ollama_already_up(monkeypatch):
    calls: list[str] = []

    def fake_start():
        calls.append("start")

    monkeypatch.setattr(orr, "ollama_api_reachable", lambda *a, **k: True)
    monkeypatch.setattr(orr, "start_ollama", fake_start)
    orr.ensure_ollama(_settings(), timeout=5)
    assert calls == []


def test_ensure_ollama_starts_and_waits(monkeypatch):
    state = {"ready": False}

    def reachable(*a, **k):
        return state["ready"]

    def fake_start():
        state["ready"] = True

    monkeypatch.setattr(orr, "ollama_api_reachable", reachable)
    monkeypatch.setattr(orr, "start_ollama", fake_start)
    orr.ensure_ollama(_settings(), timeout=5)


def test_start_ollama_via_serve(monkeypatch, tmp_path):
    cli = tmp_path / "ollama.exe"
    cli.write_text("", encoding="utf-8")
    monkeypatch.setattr(orr.shutil, "which", lambda name: None)
    monkeypatch.setattr(orr, "_ollama_cli", lambda: str(cli))

    launched: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        launched.append(list(cmd))
        return MagicMock()

    monkeypatch.setattr(orr.subprocess, "Popen", fake_popen)
    orr.start_ollama()
    assert launched == [[str(cli), "serve"]]


def test_start_ollama_missing_raises(monkeypatch):
    monkeypatch.setattr(orr, "_ollama_cli", lambda: None)
    monkeypatch.setattr(orr, "_ollama_app_paths", lambda: [])
    with pytest.raises(RuntimeError, match="Ollama не найден"):
        orr.start_ollama()
