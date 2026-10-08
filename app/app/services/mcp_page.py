"""Порции ответов проектного MCP (docs/plans/MCP-KB-PLAN.md, В1): бюджет байт на вызов, нарезка текста и списков по курсору.

Бюджет — `MCP_PAGE_BYTES` (60 КБ, ~20 тыс. токенов: помещается в лимит ответа инструмента у Claude Code и Cursor), агент может
попросить больше параметром `page_bytes` (до 200 КБ). Бюджет живёт в контекстной переменной на время одного вызова инструмента:
`contract.paginate` берёт его оттуда, поэтому файлы контракта по HTTP по-прежнему отдаются порциями до 200 КБ.
"""
from __future__ import annotations

import contextvars
import json
from contextlib import contextmanager
from typing import Any, Iterator

MIN_PAGE_BYTES = 8_000
MAX_PAGE_BYTES = 200_000
_BUDGET: contextvars.ContextVar[int | None] = contextvars.ContextVar("mcp_page_bytes", default=None)


def clamp(value: Any, default: int) -> int:
    try:
        n = int(value) if value not in (None, "") else int(default)
    except (TypeError, ValueError):
        n = int(default)
    return max(MIN_PAGE_BYTES, min(MAX_PAGE_BYTES, n))


@contextmanager
def budget(page_bytes: int) -> Iterator[int]:
    """Бюджет порции на время вызова инструмента."""
    token = _BUDGET.set(page_bytes)
    try:
        yield page_bytes
    finally:
        _BUDGET.reset(token)


def current(default: int | None = None) -> int | None:
    """Бюджет текущего вызова MCP; вне MCP — `default`."""
    value = _BUDGET.get()
    return value if value is not None else default


def size(payload: Any) -> int:
    """Размер ответа так, как его считает протокол: компактный JSON в UTF-8."""
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _int_cursor(cursor: Any) -> int:
    try:
        return max(0, int(str(cursor or 0)))
    except ValueError as exc:
        raise ValueError(f"cursor: ожидается число из прошлого ответа, получено {cursor!r}") from exc


def text_page(text: str, cursor: Any, frame: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
    """Порция текста с позиции `cursor` (символы), по границе строки, чтобы ответ целиком (`frame` + текст) уложился в `max_bytes`.

    Поля ответа: `content`, `offset`, `total_chars`, `cursor` (следующая позиция или None), `truncated` (есть продолжение)."""
    start = min(_int_cursor(cursor), len(text))
    room = max(1_000, max_bytes - size({**frame, "content": "", "offset": start, "total_chars": len(text), "cursor": str(len(text)), "truncated": True}))
    take = room // 2  # кириллица — 2 байта; экранирование уточняется ниже
    while True:
        end = min(len(text), start + max(1, take))
        if end < len(text):
            cut = text.rfind("\n", start, end)
            if cut > start + (end - start) // 2:
                end = cut + 1
        piece = text[start:end]
        page = {**frame, "content": piece, "offset": start, "total_chars": len(text),
                "cursor": str(end) if end < len(text) else None, "truncated": end < len(text)}
        if size(page) <= max_bytes or take <= 200:
            return page
        take = int(take * 0.8)


def list_page(payload: dict[str, Any], key: str, cursor: Any, *, max_bytes: int, limit: int | None = None) -> dict[str, Any]:
    """Порция списка `payload[key]` с позиции `cursor` (номер элемента) не больше `max_bytes`: `{..., total, cursor, key: [...]}`."""
    items = list(payload.get(key) or [])
    start = min(_int_cursor(cursor), len(items))
    take = min(limit or len(items), len(items) - start)
    while True:
        end = start + max(take, 0)
        page = {**payload, "total": len(items), key: items[start:end], "cursor": str(end) if end < len(items) else None}
        if size(page) <= max_bytes or take <= 1:
            return page
        take = max(1, take * 3 // 4)
