"""Автозапуск Ollama перед эмбеддингами и индексацией."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from app.app.config import Settings
from app.app.logging_setup import log_event

logger = logging.getLogger("primaryanalitics.ollama.runtime")

_SERVE_PROC: subprocess.Popen | None = None


def _ollama_app_paths() -> list[Path]:
    paths: list[Path] = []
    local = os.environ.get("LocalAppData")
    if local:
        paths.append(Path(local) / "Programs" / "Ollama" / "Ollama.exe")
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if base:
            paths.append(Path(base) / "Ollama" / "Ollama.exe")
    return paths


def _ollama_cli() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    local = os.environ.get("LocalAppData")
    if local:
        candidate = Path(local) / "Programs" / "Ollama" / "ollama.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def ollama_api_reachable(settings: Settings, *, timeout: float = 5) -> bool:
    url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(url)
            r.raise_for_status()
        return True
    except Exception:
        return False


def start_ollama() -> None:
    """Запустить Ollama на Windows (serve или приложение)."""
    global _SERVE_PROC

    cli = _ollama_cli()
    if cli:
        try:
            _SERVE_PROC = subprocess.Popen(
                [cli, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            log_event(logger, "ollama.serve.start", cli=cli)
            return
        except (subprocess.SubprocessError, OSError):
            _SERVE_PROC = None

    for path in _ollama_app_paths():
        if path.is_file():
            subprocess.Popen(
                [str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            log_event(logger, "ollama.app.start", path=str(path))
            return

    raise RuntimeError(
        "Ollama не найден. Установите Ollama или запустите его вручную."
    )


def ensure_ollama(settings: Settings, *, timeout: float = 120) -> None:
    """Дождаться API Ollama; при необходимости запустить serve/приложение."""
    if ollama_api_reachable(settings):
        return

    log_event(logger, "ollama.starting", url=settings.ollama_base_url)
    start_ollama()

    started = time.time()
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        if ollama_api_reachable(settings):
            log_event(logger, "ollama.ready", seconds=round(time.time() - started, 1))
            return
        try:
            url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
            with httpx.Client(timeout=5) as client:
                r = client.get(url)
                last_err = f"HTTP {r.status_code}"
        except Exception as exc:
            last_err = str(exc)
        time.sleep(2)

    raise RuntimeError(f"Ollama не поднялся за {int(timeout)} с: {last_err}")
