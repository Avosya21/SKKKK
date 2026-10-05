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
SCREEN_RECORDER = ROOT / "tools" / "livekit" / "screen_recorder.py"
LIVEKIT_VERSION = "1.8.4"
LIVEKIT_ZIP_NAME = f"livekit_{LIVEKIT_VERSION}_windows_amd64.zip"
LIVEKIT_ZIP_URL = (
    "https://github.com/livekit/livekit/releases/download/"
    f"v{LIVEKIT_VERSION}/{LIVEKIT_ZIP_NAME}"
)
DOCKER_WAIT_SEC = 90
DOWNLOAD_TIMEOUT_SEC = 120
PROCESS_QUERY_LIMITED = 0x1000
HEARTBEAT_STALE_SEC = 12.0


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
        [str(CADDY_EXE), "reload", "--force", "--config", str(CADDYFILE)],  # --force: при том же Caddyfile Caddy иначе не перечитывает обновлённый файл сертификата
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


def _win_pid_running(pid: int) -> bool:
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _win_pid_running(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _heartbeat_age(path: Path) -> float | None:
    """Возвращает возраст heartbeat в секундах или None, если файла нет/не читается."""
    if not path.is_file():
        return None
    try:
        stamp = float(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    return time.time() - stamp


def _kill_pid_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    else:
        try:
            os.kill(pid, 0)
            os.kill(pid, 9)
        except OSError:
            pass


# Готовность — так же, как стартует сам рекордер голоса: модуль грузится, livekit SDK на месте. Шумодав теперь в браузере,
# DeepFilterNet и torch в venvs/voice не нужны.
_VOICE_READY_CODE = (
    "import sys; sys.path.insert(0, sys.argv[1]); import voice_cleaner; "
    "from livekit import rtc"
)


def voice_venv_ready() -> bool:
    if not VOICE_PY.is_file():
        return False
    try:
        done = subprocess.run(
            [str(VOICE_PY), "-c", _VOICE_READY_CODE, str(VOICE_CLEANER.parent)],
            capture_output=True,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def edge_disabled() -> bool:
    """Тесты и CI не должны запускать настоящие рекордеры: в пустой временной папке «уже запущенных» нет, и на каждый запуск тестов
    появлялась ещё одна копия. Копии с одним именем участника выбивают друг друга из комнаты LiveKit и рвут запись созвона."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SKKKK_DISABLE_EDGE") == "1")


def ensure_voice_cleaner() -> str:
    settings = get_settings()
    if edge_disabled():
        return "off"
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


def ensure_screen_recorder() -> str:
    settings = get_settings()
    if edge_disabled():
        return "off"
    if not VOICE_PY.is_file() or not SCREEN_RECORDER.is_file():
        log_event(logger, "edge.screen.skip", reason="venv")
        return "skip"
    pid_path = settings.data_dir / "locks" / "screen_recorder.pid"
    heartbeat_path = settings.data_dir / "locks" / "screen_recorder.heartbeat"
    dead_pid = None
    if pid_path.is_file():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = 0
        if pid > 0 and _pid_running(pid):
            age = _heartbeat_age(heartbeat_path)
            if age is not None and age < HEARTBEAT_STALE_SEC:
                return "already"
            dead_pid = pid
    if not _wait_port(LIVEKIT_HTTP_PORT, timeout=10.0):
        log_event(logger, "edge.screen.skip", reason="livekit-port")
        return "skip"
    if dead_pid:
        log_event(logger, "edge.screen.restart", pid=dead_pid, reason="stale-heartbeat")
        _kill_pid_tree(dead_pid)
    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = "-1"
    env.setdefault("MEETING_RECORD", "1")
    env.setdefault("MEETING_SCREEN_RECORD", "1")
    env["LIVEKIT_RTC_INTERNAL"] = settings.livekit_rtc_internal
    env["LIVEKIT_HTTP_INTERNAL"] = "http://127.0.0.1:7880"
    log_path = settings.data_dir / "logs" / "screen_recorder.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(VOICE_PY), "-u", str(SCREEN_RECORDER)],
        cwd=str(ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.write_text(str(int(time.time())), encoding="utf-8")
    time.sleep(0.6)
    if proc.poll() is not None:
        log_event(logger, "edge.screen.failed", level=logging.WARNING, code=proc.poll())
        return "failed"
    log_event(logger, "edge.screen.ready")
    return "started"


def ensure_edge_services(settings: Settings) -> None:
    tls = ensure_tls_cert(settings)
    log_event(logger, "edge.tls", status=tls)
    caddy = ensure_caddy(settings)
    log_event(logger, "edge.caddy", status=caddy)
    try:
        livekit = ensure_livekit()
        log_event(logger, "edge.livekit", status=livekit)
    except Exception as exc:
        log_event(logger, "edge.livekit.failed", level=logging.WARNING, error=str(exc))
        return
    try:
        log_event(logger, "edge.voice", status=ensure_voice_cleaner())
    except Exception as exc:
        log_event(logger, "edge.voice.failed", level=logging.WARNING, error=str(exc))
    try:
        log_event(logger, "edge.screen", status=ensure_screen_recorder())
    except Exception as exc:
        log_event(logger, "edge.screen.failed", level=logging.WARNING, error=str(exc))


def _file_cert_serial(path: Path) -> int | None:
    from cryptography import x509

    try:
        return x509.load_pem_x509_certificate(path.read_bytes()).serial_number
    except (OSError, ValueError):
        return None


def _served_cert_serial(host: str, port: int) -> int | None:
    """Серийный номер сертификата, который порт реально отдаёт сейчас (без проверки доверия: сравниваем только с файлом)."""
    import ssl

    from cryptography import x509

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=6) as raw:
            with ctx.wrap_socket(raw, server_hostname=None) as tls:
                der = tls.getpeercert(binary_form=True)
        return x509.load_der_x509_certificate(der).serial_number if der else None
    except (OSError, ValueError, ssl.SSLError):
        return None


def check_served_certificate(settings: Settings) -> str:
    """Caddy держит сертификат :777 в памяти и не перечитывает файл при обновлении (ACME, ~раз в 6 дней).
    Сравниваем то, что отдаёт порт, с файлом на диске; при расхождении принудительно перезагружаем Caddy.
    Результат: skip | ok | reloaded | failed."""
    if not (settings.skkkk_public_host or "").strip():
        return "skip"
    cert, _key = tls_paths(settings)
    on_disk = _file_cert_serial(cert)
    if on_disk is None:
        return "skip"
    served = _served_cert_serial("127.0.0.1", settings.skkkk_https_port)
    if served == on_disk:
        return "ok"
    log_event(logger, "edge.tls.mismatch", level=logging.WARNING, served_known=served is not None)
    try:
        ensure_caddy(settings)
    except Exception as exc:  # noqa: BLE001
        log_event(logger, "edge.tls.reload.failed", level=logging.WARNING, error=str(exc))
        return "failed"
    time.sleep(1.5)
    if _served_cert_serial("127.0.0.1", settings.skkkk_https_port) == on_disk:
        log_event(logger, "edge.tls.reloaded", cert=str(cert))
        return "reloaded"
    return "failed"


def watch_edge_services() -> dict[str, str]:
    """Лёгкий watchdog: не трогает TLS/Caddy/конфиг LiveKit, только живость процессов."""
    settings = get_settings()
    statuses: dict[str, str] = {}
    if settings.meeting_voice_server:
        try:
            statuses["voice"] = ensure_voice_cleaner()
        except Exception as exc:
            log_event(logger, "edge.voice.watch.failed", level=logging.WARNING, error=str(exc))
            statuses["voice"] = "failed"
    try:
        statuses["screen"] = ensure_screen_recorder()
    except Exception as exc:
        log_event(logger, "edge.screen.watch.failed", level=logging.WARNING, error=str(exc))
        statuses["screen"] = "failed"
    return statuses
