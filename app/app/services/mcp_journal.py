"""Журнал чтения проектного MCP (docs/plans/MCP-KB-PLAN.md, Э4): кто, чем и сколько читал — без содержимого ответов.

Каждый вызов инструмента — строка `mcp.call` в api.log (проект, префикс ключа, агент, инструмент, имена параметров, байты,
миллисекунды, ошибка) и +1 в счётчике `mcp_call_stats` (ключ × день × инструмент). Значения параметров не пишутся: в них может
быть текст вопроса или поиска. Сбой журнала ответ агенту не ломает.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.logging_setup import log_event

logger = logging.getLogger("skkkk.mcp")


def record_call(project_id: int, token: Any, tool: str, args: dict[str, Any] | None, *, size: int, ms: int, error: str | None) -> None:
    from sqlalchemy.dialects.sqlite import insert

    from app.app.db import get_engine
    from app.app.models.project_mcp_token import McpCallStat

    token_id = getattr(token, "id", None)
    log_event(logger, "mcp.call", project=project_id, key=getattr(token, "token_prefix", None), agent=getattr(token, "agent_name", None),
              tool=tool, args=",".join(sorted(args or {})), bytes=size, ms=ms, error=error)
    if not token_id:
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stmt = insert(McpCallStat).values(project_id=project_id, token_id=token_id, day=day, tool=str(tool)[:48], calls=1, bytes_out=size,
                                      errors=1 if error else 0)
    stmt = stmt.on_conflict_do_update(index_elements=["token_id", "day", "tool"],
                                      set_={"calls": McpCallStat.calls + 1, "bytes_out": McpCallStat.bytes_out + size,
                                            "errors": McpCallStat.errors + (1 if error else 0)})
    try:
        with get_engine().begin() as conn:
            conn.execute(stmt)
    except Exception:  # noqa: BLE001 — журнал не должен ломать ответ агенту
        logger.warning("mcp.call: счётчик не записан", exc_info=True)


def key_stats(db: Session, project_id: int, *, days: int = 7) -> dict[int, dict[str, Any]]:
    """По ключам проекта за последние `days` дней: вызовы, байты, ошибки, частые инструменты."""
    from app.app.models.project_mcp_token import McpCallStat

    since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = db.execute(
        select(McpCallStat.token_id, McpCallStat.tool, func.sum(McpCallStat.calls), func.sum(McpCallStat.bytes_out), func.sum(McpCallStat.errors))
        .where(McpCallStat.project_id == project_id, McpCallStat.day >= since)
        .group_by(McpCallStat.token_id, McpCallStat.tool)
    ).all()
    out: dict[int, dict[str, Any]] = {}
    for token_id, tool, calls, size, errors in rows:
        row = out.setdefault(token_id, {"calls": 0, "bytes": 0, "errors": 0, "tools": {}})
        row["calls"] += int(calls or 0)
        row["bytes"] += int(size or 0)
        row["errors"] += int(errors or 0)
        row["tools"][tool] = int(calls or 0)
    for row in out.values():
        row["top_tools"] = [t for t, _ in sorted(row.pop("tools").items(), key=lambda kv: -kv[1])[:5]]
    return out
