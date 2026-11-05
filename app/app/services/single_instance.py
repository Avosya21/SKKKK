"""Защита от запуска второго GPU-воркера.

По инварианту проекта воркер ровно один. Второй экземпляр разбирает ту же
очередь и параллельно пишет в SQLite, а SQLite допускает одного писателя —
на длинных вызовах LLM это выливается в `database is locked`.

Блокировку держит ОС на дескрипторе файла, поэтому она снимается сама даже
после аварийного завершения процесса, в отличие от обычного pid-файла.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("primaryanalitics.lock")


class AlreadyRunning(RuntimeError):
    pass


def _lock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SingleInstance:
    def __init__(self, name: str, lock_dir: Path) -> None:
        lock_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = lock_dir / f"{name}.lock"
        # PID лежит в отдельном файле: заблокированный байт .lock не прочитать.
        self.pid_path = lock_dir / f"{name}.pid"
        self._handle = None

    def _owner(self) -> str:
        try:
            return self.pid_path.read_text(encoding="utf-8").strip() or "неизвестен"
        except OSError:
            return "неизвестен"

    def __enter__(self) -> SingleInstance:
        handle = self.lock_path.open("a+")
        handle.seek(0)
        try:
            _lock(handle)
        except OSError as exc:
            handle.close()
            raise AlreadyRunning(
                f"уже запущен другой экземпляр (pid {self._owner()}), "
                f"файл блокировки {self.lock_path}"
            ) from exc
        self._handle = handle
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        return self

    def __exit__(self, *_exc) -> None:
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        except OSError:
            pass
        self._handle.close()
        self._handle = None
        self.pid_path.unlink(missing_ok=True)
