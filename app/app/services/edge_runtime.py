"""HTTPS (Caddy) and LiveKit room — поднимает main вместе с порталом."""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

from app.app.config import Settings, get_settings
from app.app.logging_setup import log_event
from app.app.services.tls_cert import ensure_tls_cert, tls_paths
from app.app.services.livekit_env import (
    RTC_TCP_PORT,
    RTC_UDP_END,
    RTC_UDP_START,
    TURN_UDP_PORT,
    YAML_PATH,
    ensure_livekit_files,
)

logger = logging.getLogger("skkkk.edge")

ROOT = Path(__file__).resolve().parents[3]
CADDY_DIR = ROOT / "tools" / "caddy"
CADDY_EXE = CADDY_DIR / "caddy.exe"
CADDYFILE = CADDY_DIR / "Caddyfile"
LIVEKIT_DIR = ROOT / "tools" / "livekit"
LIVEKIT_COMPOSE = LIVEKIT_DIR / "docker-compose.yml"
LIVEKIT_EXE = LIVEKIT_DIR / "livekit-server.exe"
LIVEKIT_CONTAINER = "skkkk-livekit"
LIVEKIT_HTTP_PORT = 7880
VOICE_PY = ROOT / "venvs" / "voice" / "Scripts" / "python.exe"
VOICE_CLEANER = ROOT / "tools" / "livekit" / "voice_cleaner.py"
LIVEKIT_VERSION = "1.8.4"
LIVEKIT_ZIP_NAME = f"livekit_{LIVEKIT_VERSION}_windows_amd64.zip"
LIVEKIT_ZIP_URL = (
    "https://github.com/livekit/livekit/releases/download/"
    f"v{LIVEKIT_VERSION}/{LIVEKIT_ZIP_NAME}"
)
DOCKER_WAIT_SEC = 90
DOWNLOAD_TIMEOUT_SEC = 120


_CADDY_FILE_KEYS = (
    "SKKKK_TLS_CERT",
    "SKKKK_TLS_KEY",
    "SKKKK_PUBLIC_HOST",
    "SKKKK_ACME_EMAIL",
    "SKKKK_ACME_CA",
    "SKKKK_HTTPS_PORT",
    "SKKKK_ACME_HTTP_PORT",
    "SKKKK_ACME_TLS_PORT",
)


def _caddy_env(settings: Settings) -> dict[str, str]:
    from app.app.services.livekit_env import ENV_PATH, _read_env

    env = dict(os.environ)
    file_env = _read_env(ENV_PATH)
    for key in _CADDY_FILE_KEYS:
        if file_env.get(key):
            env[key] = file_env[key]
    env["SKKKK_PUBLIC_HOST"] = settings.skkkk_public_host or env.get("SKKKK_PUBLIC_HOST", "")
    env["SKKKK_HTTPS_PORT"] = str(settings.skkkk_https_port)
    env["SKKKK_ACME_HTTP_PORT"] = str(settings.skkkk_acme_http_port)
    env["SKKKK_ACME_EMAIL"] = settings.skkkk_acme_email or env.get("SKKKK_ACME_EMAIL", "")
    env["SKKKK_ACME_CA"] = settings.skkkk_acme_ca or env.get("SKKKK_ACME_CA", "")
    env["SKKKK_WEB_PORT"] = str(settings.web_port)
    env["SKKKK_ACME_TLS_PORT"] = str(settings.skkkk_acme_tls_port)
    env["SKKKK_CADDY_STORAGE"] = str(settings.data_dir / "caddy")
    env["SKKKK_CADDY_LOG"] = str(settings.data_dir / "logs" / "caddy.log")
    if (settings.skkkk_public_host or "").strip():
        cert, key = tls_paths(settings)
        env["SKKKK_TLS_CERT"] = str(cert)
        env["SKKKK_TLS_KEY"] = str(key)
    return env


def ensure_caddy(settings: Settings) -> str:
    if not CADDY_EXE.is_file() or not CADDYFILE.is_file():
        log_event(logger, "edge.caddy.skip", reason="binary_or_config_missing")
        return "skip"
    env = _caddy_env(settings)
    reload = subprocess.run(
        [str(CADDY_EXE), "reload", "--config", str(CADDYFILE)],
        cwd=str(CADDY_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if reload.returncode == 0:
        log_event(logger, "edge.caddy.reloaded")
        return "reloaded"
    subprocess.Popen(
        [str(CADDY_EXE), "start", "--config", str(CADDYFILE)],
        cwd=str(CADDY_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log_event(logger, "edge.caddy.started")
    return "started"


def _open_livekit_firewall() -> None:
    rules = (
        ("SKKKK LiveKit TCP ICE", "TCP", str(RTC_TCP_PORT)),
        ("SKKKK LiveKit TCP TURN", "TCP", str(TURN_UDP_PORT)),
        ("SKKKK LiveKit UDP TURN", "UDP", str(TURN_UDP_PORT)),
        ("SKKKK LiveKit UDP media", "UDP", f"{RTC_UDP_START}-{RTC_UDP_END}"),
    )
    for name, proto, port in rules:
        subprocess.run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={name}",
                "dir=in",
                "action=allow",
                f"protocol={proto}",
                f"localport={port}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )


def _port_open(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.4)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def ensure_livekit_binary() -> Path | None:
    if LIVEKIT_EXE.is_file():
        return LIVEKIT_EXE
    LIVEKIT_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = LIVEKIT_DIR / LIVEKIT_ZIP_NAME
    try:
        urllib.request.urlretrieve(LIVEKIT_ZIP_URL, zip_path)
        with zipfile.ZipFile(zip_path) as archive:
            names = [name for name in archive.namelist() if name.endswith("livekit-server.exe")]
            if not names:
                raise RuntimeError("в архиве LiveKit нет livekit-server.exe")
            archive.extract(names[0], LIVEKIT_DIR)
            extracted = LIVEKIT_DIR / names[0]
            if extracted.resolve() != LIVEKIT_EXE.resolve():
                extracted.replace(LIVEKIT_EXE)
    except Exception as exc:
        log_event(logger, "edge.livekit.binary.failed", level=logging.WARNING, error=str(exc))
        return None
    finally:
        if zip_path.is_file():
            zip_path.unlink(missing_ok=True)
    return LIVEKIT_EXE if LIVEKIT_EXE.is_file() else None


def _stop_docker_livekit() -> None:
    if not LIVEKIT_COMPOSE.is_file():
        return
    subprocess.run(
        ["docker", "compose", "-f", str(LIVEKIT_COMPOSE), "stop"],
        cwd=str(LIVEKIT_DIR),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    for _ in range(20):
        if not _port_open(LIVEKIT_HTTP_PORT):
            return
        time.sleep(0.25)


def _wait_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port):
            return True
        time.sleep(0.2)
    return False


def _yaml_sha() -> str:
    if not YAML_PATH.is_file():
        return ""
    return hashlib.sha256(YAML_PATH.read_bytes()).hexdigest()


def _yaml_stamp_path() -> Path:
    return get_settings().data_dir / "locks" / "livekit.yaml.sha256"


def _yaml_stamp_matches() -> bool:
    stamp = _yaml_stamp_path()
    return stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == _yaml_sha()


def _write_yaml_stamp() -> None:
    path = _yaml_stamp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_yaml_sha() + "\n", encoding="utf-8")


def _stop_native_livekit() -> None:
    subprocess.run(
        ["taskkill", "/IM", "livekit-server.exe", "/F"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    deadline = time.time() + 5
    while _port_open(LIVEKIT_HTTP_PORT) and time.time() < deadline:
        time.sleep(0.2)


def _spawn_native_livekit(exe: Path) -> None:
    log_path = get_settings().data_dir / "logs" / "livekit.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    subprocess.Popen(
        [str(exe), "--config", str(YAML_PATH)],
        cwd=str(LIVEKIT_DIR),
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    if not _wait_port(LIVEKIT_HTTP_PORT):
        raise RuntimeError("LiveKit не открыл :7880")
    _write_yaml_stamp()


def _start_native_livekit(exe: Path) -> str:
    running = _port_open(LIVEKIT_HTTP_PORT)
    if running and _yaml_stamp_matches():
        log_event(logger, "edge.livekit.ready", mode="native", status="already")
        return "already"
    if running:
        _stop_native_livekit()
        _spawn_native_livekit(exe)
        log_event(logger, "edge.livekit.ready", mode="native", status="reloaded")
        return "reloaded"
    _spawn_native_livekit(exe)
    log_event(logger, "edge.livekit.ready", mode="native")
    return "started"


def _start_docker_livekit() -> str:
    from app.app.services.docker_runtime import docker_daemon_running, ensure_docker_daemon

    if not docker_daemon_running():
        ensure_docker_daemon(timeout=DOCKER_WAIT_SEC)
    if not LIVEKIT_COMPOSE.is_file():
        raise RuntimeError(f"нет {LIVEKIT_COMPOSE}")
    proc = subprocess.run(
        ["docker", "compose", "-f", str(LIVEKIT_COMPOSE), "up", "-d"],
        cwd=str(LIVEKIT_DIR),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        raise RuntimeError(f"livekit compose: {tail}")
    log_event(logger, "edge.livekit.ready", container=LIVEKIT_CONTAINER)
    return "started"


def ensure_livekit() -> str:
    ensure_livekit_files()
    _open_livekit_firewall()
    get_settings.cache_clear()
    exe = ensure_livekit_binary()
    if exe:
        _stop_docker_livekit()
        return _start_native_livekit(exe)
    return _start_docker_livekit()


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def voice_venv_ready() -> bool:
    if not VOICE_PY.is_file():
        return False
    try:
        done = subprocess.run(
            [str(VOICE_PY), "-c", "import torchaudio; import df.enhance"],
            capture_output=True,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def ensure_voice_cleaner() -> str:
    settings = get_settings()
    if not settings.meeting_voice_server:
        return "off"
    if not VOICE_PY.is_file() or not VOICE_CLEANER.is_file():
        log_event(logger, "edge.voice.skip", reason="venv")
        return "skip"
    pid_path = settings.data_dir / "locks" / "voice_cleaner.pid"
    if pid_path.is_file():
        try:
            if _pid_running(int(pid_path.read_text(encoding="utf-8").strip())):
                return "already"
        except ValueError:
            pass
    if not _wait_port(LIVEKIT_HTTP_PORT, timeout=10.0):
        log_event(logger, "edge.voice.skip", reason="livekit-port")
        return "skip"
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = "-1"
    env["MEETING_VOICE_SERVER"] = "1"
    env.setdefault("MEETING_RECORD", "1")
    env["LIVEKIT_RTC_INTERNAL"] = settings.livekit_rtc_internal
    env["LIVEKIT_HTTP_INTERNAL"] = "http://127.0.0.1:7880"
    log_path = settings.data_dir / "logs" / "voice_cleaner.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(VOICE_PY), "-u", str(VOICE_CLEANER)],
        cwd=str(ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.6)
    if proc.poll() is not None:
        log_event(logger, "edge.voice.failed", level=logging.WARNING, code=proc.poll())
        return "failed"
    log_event(logger, "edge.voice.ready")
    return "started"


def ensure_edge_services(settings: Settings) -> None:
    tls = ensure_tls_cert(settings)
    log_event(logger, "edge.tls", status=tls)
    caddy = ensure_caddy(settings)
    log_event(logger, "edge.caddy", status=caddy)
    try:
        livekit = ensure_livekit()
        log_event(logger, "edge.livekit", status=livekit)
        log_event(logger, "edge.voice", status=ensure_voice_cleaner())
    except Exception as exc:
        log_event(logger, "edge.livekit.failed", level=logging.WARNING, error=str(exc))
