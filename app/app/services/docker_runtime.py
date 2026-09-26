"""Автозапуск Docker Desktop и ASR-контейнеров перед обработкой аудио."""

from __future__ import annotations

import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from app.app.logging_setup import log_event

logger = logging.getLogger("skkkk.docker")

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = ROOT / "asr" / "docker-compose.yml"
ENV_FILE = ROOT / ".env"

ASR_CONTAINERS: tuple[str, ...] = ("skkkk-gigaam",)
GIGAAM_CONTAINER = "skkkk-gigaam"
GIGAAM_HEALTH_WAIT_SEC = 180
GIGAAM_HEALTH_POLL_SEC = 3


def docker_daemon_running() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0


def _docker_desktop_paths() -> list[Path]:
    paths: list[Path] = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if base:
            paths.append(Path(base) / "Docker" / "Docker" / "Docker Desktop.exe")
    local = os.environ.get("LocalAppData")
    if local:
        paths.append(Path(local) / "Programs" / "Docker" / "Docker Desktop.exe")
    return paths


def start_docker_desktop() -> None:
    """Запустить Docker Desktop на Windows (CLI или exe)."""
    try:
        proc = subprocess.run(
            ["docker", "desktop", "start"],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if proc.returncode == 0:
            log_event(logger, "docker.desktop.cli_start")
            return
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    for path in _docker_desktop_paths():
        if path.is_file():
            subprocess.Popen(
                [str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            log_event(logger, "docker.desktop.exe_start", path=str(path))
            return

    raise RuntimeError(
        "Docker Desktop не найден. Установите Docker Desktop или запустите его вручную."
    )


def ensure_docker_daemon(*, timeout: float = 300) -> None:
    """Дождаться готовности docker daemon; при необходимости запустить Docker Desktop."""
    if docker_daemon_running():
        return

    log_event(logger, "docker.daemon.starting")
    start_docker_desktop()

    started = time.time()
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        if docker_daemon_running():
            log_event(logger, "docker.daemon.ready", seconds=round(time.time() - started, 1))
            return
        try:
            proc = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            last_err = (proc.stderr or proc.stdout or "").strip()[-200:]
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            last_err = str(exc)
        time.sleep(3)

    raise RuntimeError(f"Docker daemon не поднялся за {int(timeout)} с: {last_err}")


def _container_exists(name: str) -> bool:
    proc = subprocess.run(
        ["docker", "inspect", name],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return proc.returncode == 0


def _compose_up() -> None:
    if not COMPOSE_FILE.is_file():
        raise RuntimeError(f"нет {COMPOSE_FILE}")

    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    if ENV_FILE.is_file():
        cmd.extend(["--env-file", str(ENV_FILE)])
    cmd.extend(["up", "-d", "gigaam"])

    proc = subprocess.run(
        cmd,
        cwd=str(COMPOSE_FILE.parent),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise RuntimeError(f"docker compose up не удался: {tail}")


def ensure_asr_containers(*, restart: bool = True) -> None:
    """Docker daemon + контейнер GigaAM (Whisper/T-one только с profile legacy)."""
    ensure_docker_daemon()

    missing = [name for name in ASR_CONTAINERS if not _container_exists(name)]
    if missing:
        log_event(logger, "docker.asr.compose_up", missing=missing)
        _compose_up()
        still_missing = [name for name in ASR_CONTAINERS if not _container_exists(name)]
        if still_missing:
            raise RuntimeError(f"контейнеры не созданы: {', '.join(still_missing)}")

    action = "restart" if restart else "start"
    proc = subprocess.run(
        ["docker", action, *ASR_CONTAINERS],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        raise RuntimeError(f"docker {action} ASR: {tail}")
    log_event(logger, "docker.asr.ready", action=action)


def ensure_container(name: str, *, restart: bool = False) -> None:
    """Один контейнер: daemon + compose (если ASR) + start/restart."""
    ensure_docker_daemon()
    if not _container_exists(name):
        if name in ASR_CONTAINERS:
            log_event(logger, "docker.container.compose_up", container=name)
            _compose_up()
        if not _container_exists(name):
            raise RuntimeError(f"контейнер {name} не найден")
    action = "restart" if restart else "start"
    proc = subprocess.run(
        ["docker", action, name],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        raise RuntimeError(f"docker {action} {name}: {tail}")


def ensure_gigaam_ready(base_url: str, *, timeout_sec: float = GIGAAM_HEALTH_WAIT_SEC) -> None:
    ensure_container(GIGAAM_CONTAINER, restart=False)
    health = base_url.rstrip("/") + "/health"
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(health, timeout=5).read()
            return
        except Exception:
            time.sleep(GIGAAM_HEALTH_POLL_SEC)
    raise RuntimeError(f"GigaAM не запущен — контейнер {GIGAAM_CONTAINER} ({base_url})")


def stop_gigaam() -> None:
    """Остановить контейнер GigaAM после того, как он отработал, чтобы он не держал VRAM.

    Ошибки молчаливо игнорируются: контейнер мог уже быть остановлен/удалён.
    """
    try:
        proc = subprocess.run(
            ["docker", "stop", GIGAAM_CONTAINER],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode == 0:
            log_event(logger, "docker.asr.stopped", container=GIGAAM_CONTAINER)
    except Exception:
        pass
