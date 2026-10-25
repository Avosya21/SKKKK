"""Единая настройка логов: консоль (PyCharm) плюс файл в `data/logs`.

Раньше каждый вход настраивал `logging.basicConfig` только с `filename`, поэтому
в консоли не было ничего, кроме собственных строк uvicorn.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Any

_CONFIGURED: set[str] = set()

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-16s %(message)s"
_DATEFMT = "%H:%M:%S"

# Значения, которые нельзя писать в лог ни при каких условиях.
_SECRET_KEYS = {"password", "passwd", "secret", "token", "authorization", "api_key"}


def setup_logging(component: str, log_dir, level: int = logging.INFO) -> None:
    """Подключить консольный и файловый обработчики один раз на процесс.

    Повторный вызов игнорируется целиком: иначе каждая строка дублировалась бы
    по числу добавленных обработчиков.
    """
    if _CONFIGURED:
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_dir / f"{component}.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in (console, file_handler):
        root.addHandler(handler)

    # Сообщения uvicorn уже выводятся его собственными обработчиками.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).propagate = False

    # Библиотеки логируют каждый HTTP-запрос и забивают полезные события.
    for name in ("httpx", "httpcore", "urllib3", "PIL"):
        logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger("primaryanalitics").setLevel(level)
    _CONFIGURED.add(component)


def _render(value: Any) -> str:
    text = "-" if value is None else str(value)
    text = text.replace("\n", " ").strip()
    if len(text) > 120:
        text = text[:117] + "..."
    return f'"{text}"' if " " in text else text


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Записать бизнес-событие одной строкой вида `event key=value`."""
    safe = {k: v for k, v in fields.items() if k.lower() not in _SECRET_KEYS}
    tail = " ".join(f"{k}={_render(v)}" for k, v in safe.items())
    logger.log(level, "%-24s %s", event, tail)
