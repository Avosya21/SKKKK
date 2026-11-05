"""Две полосы локальной модели (план docs/plans/LLM-LANES-PLAN.md).

Фоновая — воркер (расшифровки, документы, автопроверки, выжимки, кадры, процессы ASR): не больше одного запроса.
Полоса чата — процесс API (ответы чата и черновики MCP): строго по очереди прихода, не больше одного запроса.
Модель загружена с двумя потоками и общим окном (`kv_unified`), поэтому одновременно к ней идёт не больше двух запросов.
Фоновый запрос, который не помещается в свою долю окна, берёт и полосу чата: дожидается текущего ответа чата,
и следующий ответ не начнётся, пока он не закончится.

Полосы — межпроцессные файловые замки в data/locks; очередь чата — талоны внутри процесса API (он один).
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from app.app.config import Settings
from app.app.logging_setup import log_event

logger = logging.getLogger("primaryanalitics.llm_lanes")

LANE_BACKGROUND = "background"
LANE_CHAT = "chat"
LANE_FILES = {LANE_BACKGROUND: "llm_lane_background.lock", LANE_CHAT: "llm_lane_chat.lock"}
STRICT_ENV = "PRIMARY_ANALITICS_STRICT_LLM_LANE"
# Символов на токен для русского текста промптов: замер 2026-10-05 по prompt_tokens — 3,05–3,14; берём с запасом.
CHARS_PER_TOKEN = 2.8

_lane: contextvars.ContextVar[str] = contextvars.ContextVar("llm_lane", default=LANE_BACKGROUND)
ROLE_ENV = "PRIMARY_ANALITICS_PROCESS_ROLE"  # `api` ставит main._serve: фоновые вызовы модели в процессе портала — ошибка схемы


def process_role() -> str:
    return os.environ.get(ROLE_ENV, "worker")


def current_lane() -> str:
    return _lane.get()


def estimate_tokens(*texts: str) -> int:
    return int(sum(len(t or "") for t in texts) / CHARS_PER_TOKEN) + 1


def _locks_dir(settings: Settings) -> Path:
    path = settings.data_dir / "locks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_lock(settings: Settings, lane: str):
    from filelock import FileLock

    return FileLock(str(_locks_dir(settings) / LANE_FILES[lane]))


def lanes_idle(settings: Settings) -> bool:
    """Обе полосы свободны и в очереди чата никто не ждёт (для выгрузки модели по простою)."""
    if chat_queue.waiting():
        return False
    for lane in LANE_FILES:
        lock = _file_lock(settings, lane)
        try:
            lock.acquire(timeout=0)
        except Exception:  # noqa: BLE001 — Timeout: полосу держит запрос
            return False
        lock.release()
    return True


# --- очередь чата (в процессе API) ----------------------------------------------------------------------------------

class ChatQueue:
    """FIFO по талонам: ответы чата и черновики MCP выполняются строго в порядке прихода, по одному."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._next = 0
        self._serving = 0
        self._owners: dict[int, Any] = {}
        self._loading: set[int] = set()

    def take(self, owner: Any) -> int:
        with self._cond:
            ticket = self._next
            self._next += 1
            self._owners[ticket] = owner
            return ticket

    def wait_turn(self, ticket: int) -> None:
        with self._cond:
            while self._serving != ticket:
                self._cond.wait()

    def done(self, ticket: int) -> None:
        with self._cond:
            self._owners.pop(ticket, None)
            self._loading.discard(ticket)
            if self._serving == ticket:
                self._serving += 1
                # талоны брошенных запросов (исключение до wait_turn) пропускаем
                while self._serving < self._next and self._serving not in self._owners:
                    self._serving += 1
            self._cond.notify_all()

    def waiting(self) -> int:
        with self._cond:
            return len(self._owners)

    def position(self, owner: Any) -> int | None:
        """0 — сейчас отвечает, N — перед ним N запросов; None — в очереди нет."""
        with self._cond:
            mine = [t for t, o in self._owners.items() if o == owner]
            if not mine:
                return None
            return sum(1 for t in self._owners if t < mine[0])

    def set_loading(self, ticket: int, flag: bool) -> None:
        with self._cond:
            (self._loading.add if flag else self._loading.discard)(ticket)

    def loading(self, owner: Any) -> bool:
        with self._cond:
            return any(o == owner and t in self._loading for t, o in self._owners.items())


chat_queue = ChatQueue()
_turn_ticket: contextvars.ContextVar[int | None] = contextvars.ContextVar("chat_ticket", default=None)


@contextmanager
def chat_turn(owner: Any, *, label: str = "chat") -> Iterator[None]:
    """Очередь полосы чата: дождаться своей очереди, затем все вызовы модели внутри идут полосой чата."""
    ticket = chat_queue.take(owner)
    started = time.monotonic()
    try:
        chat_queue.wait_turn(ticket)
        waited = time.monotonic() - started
        if waited > 1:
            log_event(logger, "llm.chat_queue.turn", label=label, waited=round(waited, 1))
        lane_token = _lane.set(LANE_CHAT)
        ticket_token = _turn_ticket.set(ticket)
        try:
            yield
        finally:
            _turn_ticket.reset(ticket_token)
            _lane.reset(lane_token)
    finally:
        chat_queue.done(ticket)


def _asr_hold_active() -> bool:
    from app.app.config import get_settings
    from app.app.services.llm_keeper import asr_hold_active

    try:
        return asr_hold_active(get_settings())
    except Exception:  # noqa: BLE001 — подпись не обязательна
        return False


def chat_status(owner: Any) -> dict[str, Any]:
    position = chat_queue.position(owner)
    if position is None:
        return {"queued": False}
    if position == 0:
        if chat_queue.loading(owner):
            label = (
                "Идёт распознавание речи — ИИ ответит сразу после этого шага…"
                if _asr_hold_active()
                else "Загружаю модель…"
            )
        else:
            label = "ИИ готовит ответ…"
    else:
        label = f"В очереди к ИИ: перед вами {position}"
    return {"queued": True, "position": position, "label": label}


# --- запрос к модели ------------------------------------------------------------------------------------------------

def background_budget(settings: Settings, context_length: int) -> int:
    return max(0, context_length - settings.lmstudio_chat_token_budget)


@contextmanager
def llm_request(settings: Settings, *, est_tokens: int, what: str) -> Iterator[None]:
    """Обёртка каждого HTTP-вызова LM Studio: модель загружена по профилю, полоса занята, простой отмечен."""
    from app.app.services import llm_keeper

    lane = _lane.get()
    if process_role() == "api" and lane == LANE_BACKGROUND:
        message = f"фоновый вызов модели из процесса API ({what}): должен идти задачей очереди воркера"
        if os.environ.get(STRICT_ENV) == "1":
            raise RuntimeError(message)
        log_event(logger, "llm.lane.violation", level=logging.WARNING, what=what)
    ticket = _turn_ticket.get()
    with ExitStack() as stack:
        if ticket is not None:  # для подписи в чате: ждём полосу (GPU-шаг ASR) или загрузку модели
            chat_queue.set_loading(ticket, True)
            stack.callback(chat_queue.set_loading, ticket, False)
        stack.enter_context(_file_lock(settings, lane))
        if lane == LANE_BACKGROUND:
            try:
                context = llm_keeper.load_profile().context_length
            except Exception:  # noqa: BLE001
                context = 0
            if context and est_tokens > background_budget(settings, context):
                started = time.monotonic()
                stack.enter_context(_file_lock(settings, LANE_CHAT))
                log_event(
                    logger, "llm.lane.exclusive", what=what, est_tokens=est_tokens,
                    budget=background_budget(settings, context), waited=round(time.monotonic() - started, 1),
                )
        # Загрузка — уже под замком полосы: GPU-шаг ASR (asr_gpu_hold) берёт обе полосы и не выгрузит модель между
        # проверкой и запросом.
        llm_keeper.ensure_loaded(settings, reason=f"{lane}:{what}")
        if ticket is not None:
            chat_queue.set_loading(ticket, False)
        llm_keeper.touch_last_use(settings)
        try:
            yield
        finally:
            llm_keeper.touch_last_use(settings)
