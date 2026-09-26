"""Признак «код на диске новее запущенного сервера» (пункт плана 1.3).

Серверы стартуют без --reload, поэтому правки .py, шаблонов и .env не действуют до ручного
перезапуска. Модуль запоминает момент старта и сравнивает с датами изменения файлов.
Статика (app/static) сюда не входит: она отдаётся с диска без перезапуска.
"""
from __future__ import annotations

import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
APP_DIR = ROOT / "app" / "app"
ENV_FILE = ROOT / ".env"
WORKER_PID_FILE = ROOT / "data" / "locks" / "gpu_worker.pid"

_SKIP_PARTS = {"__pycache__", "tests", "static"}
_CACHE_SEC = 5.0
_cache: tuple[float, list[tuple[float, str]]] | None = None

STARTED_AT = time.time()


def _watched_files() -> list[Path]:
    files: list[Path] = []
    for pattern in ("*.py", "*.html"):
        for path in APP_DIR.rglob(pattern):
            if not _SKIP_PARTS.intersection(path.relative_to(APP_DIR).parts):
                files.append(path)
    if ENV_FILE.is_file():
        files.append(ENV_FILE)
    return files


def _mtimes() -> list[tuple[float, str]]:
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < _CACHE_SEC:
        return _cache[1]
    rows: list[tuple[float, str]] = []
    for path in _watched_files():
        try:
            rows.append((path.stat().st_mtime, path.relative_to(ROOT).as_posix()))
        except OSError:
            continue
    rows.sort(reverse=True)
    _cache = (now, rows)
    return rows


def _changed_since(moment: float, limit: int = 5) -> list[str]:
    return [name for mtime, name in _mtimes() if mtime > moment][:limit]


def worker_started_at() -> float | None:
    try:
        return WORKER_PID_FILE.stat().st_mtime
    except OSError:
        return None


def build_status() -> dict:
    api_changed = _changed_since(STARTED_AT)
    worker_started = worker_started_at()
    worker_changed = _changed_since(worker_started) if worker_started else []
    return {
        "api_started_at": STARTED_AT,
        "api_stale": bool(api_changed),
        "api_changed": api_changed,
        "worker_started_at": worker_started,
        "worker_stale": bool(worker_changed),
        "worker_changed": worker_changed,
        "stale": bool(api_changed or worker_changed),
    }
