"""Лёгкий монитор SKKKK: health, очередь, ошибки job — пишет в data/logs/monitor_loop.log."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "logs" / "monitor_loop.log"
sys.path.insert(0, str(ROOT))


def _log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def _health() -> dict | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:777/health", timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        _log(f"HEALTH_FAIL {exc}")
        return None


def _recent_jobs(limit: int = 3) -> list[str]:
    from sqlalchemy import select

    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.job import Job

    lines: list[str] = []
    db = SessionLocal()
    try:
        jobs = db.scalars(select(Job).order_by(Job.id.desc()).limit(limit)).all()
        for j in jobs:
            art = db.get(Artifact, j.artifact_id)
            name = (art.original_filename[:36] if art else "?")
            st = art.status if art else "?"
            err = (j.error_message or (art.error_message if art else "") or "")[:120]
            lines.append(f"job#{j.id} {j.status}/{j.phase} art={st} {name!r} {err}")
    finally:
        db.close()
    return lines


def tick() -> None:
    h = _health()
    if h:
        q = h.get("queue") or {}
        _log(
            "HEALTH_OK "
            f"llm={h.get('llm_provider')} ok={h.get('llm')} "
            f"queue={q.get('queued', 0)} running={q.get('running', 0)} vram={h.get('vram_mib')}MiB"
        )
    for line in _recent_jobs():
        if "error" in line or "running" in line:
            _log(f"JOB {line}")


if __name__ == "__main__":
    tick()
