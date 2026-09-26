"""Ограничитель неудачных попыток (пункт плана 4.7): защита входа от перебора паролей ролей.

Скользящее окно в памяти процесса: после `limit` неудач за `window_sec` ключ блокируется до истечения окна.
Успешный вход сбрасывает счётчик имени (не IP). Состояние не переживает перезапуск — этого достаточно
против онлайн-перебора; для распределённой атаки нужен внешний фильтр (Caddy/файрвол).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class FailureLimiter:
    def __init__(self, limit: int, window_sec: float) -> None:
        self.limit = limit
        self.window = window_sec
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque[float]:
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
            return self._hits[key]
        return hits

    def retry_after(self, key: str, limit: int | None = None) -> int:
        """Секунд до разблокировки; 0 — можно пробовать. `limit` — свой лимит для этого ключа."""
        now = time.monotonic()
        with self._lock:
            hits = self._trim(key, now)
            if len(hits) < (limit or self.limit):
                return 0
            return max(1, int(self.window - (now - hits[0])) + 1)

    def remaining(self, key: str, limit: int | None = None) -> int:
        now = time.monotonic()
        with self._lock:
            return max(0, (limit or self.limit) - len(self._trim(key, now)))

    def fail(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._trim(key, now).append(now)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


# Вход: 8 неудач за 5 минут на IP и на логин (перебор одного логина с разных IP тоже режется).
LOGIN_LIMITER = FailureLimiter(limit=8, window_sec=300)
