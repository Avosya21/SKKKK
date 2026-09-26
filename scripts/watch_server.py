"""Watch SKKKK server, queue, and pipeline errors (stdout + monitor_loop.log)."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "app.db"
LOG = ROOT / "data" / "logs" / "monitor_loop.log"
HEALTH_URL = "http://127.0.0.1:777/health"
INTERVAL_SEC = 30


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def health() -> dict | None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=8) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        log(f"HEALTH_FAIL {exc}")
        return None


def db_snapshot() -> str:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    jobs = conn.execute(
        """
        SELECT j.id, j.status, a.artifact_id, a.status AS art_status, a.original_filename
        FROM jobs j JOIN artifacts a ON j.artifact_id = a.id
        WHERE j.status IN ('queued', 'running')
        ORDER BY j.id DESC LIMIT 3
        """
    ).fetchall()
    arts = conn.execute(
        """
        SELECT artifact_id, status, substr(error_message, 1, 120) AS err
        FROM artifacts
        WHERE status NOT IN ('done') AND project_id = 9
        ORDER BY id DESC LIMIT 3
        """
    ).fetchall()
    conn.close()
    parts = []
    for j in jobs:
        parts.append(
            f"job#{j['id']} {j['status']} art={j['artifact_id']} ({j['art_status']}) "
            f"{j['original_filename'][:50]}"
        )
    for a in arts:
        if a["status"] == "error":
            parts.append(f"ERROR {a['artifact_id']}: {a['err']}")
    return " | ".join(parts) if parts else "idle"


def checklist_step(artifact_id: str) -> str:
    path = ROOT / "data" / "projects" / "9" / "artifacts" / artifact_id / "asr" / "pipeline_checklist.json"
    if not path.is_file():
        return "checklist:—"
    data = json.loads(path.read_text(encoding="utf-8"))
    steps = data.get("steps") or {}
    running = [k for k, v in steps.items() if v.get("status") == "running"]
    if running:
        return f"step={running[0]}"
    pending = [k for k, v in steps.items() if v.get("status") == "pending"]
    ok = sum(1 for v in steps.values() if v.get("status") == "ok")
    return f"ok={ok}/{len(steps)} next={pending[0] if pending else 'done'}"


def tail_errors() -> list[str]:
    found: list[str] = []
    for name in ("worker.log", "api.log"):
        path = ROOT / "data" / "logs" / name
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
        for ln in lines:
            if any(x in ln for x in ("ERROR", " FAIL ", "Traceback", "job.failed", "asr.failed")):
                found.append(f"{name}: {ln.strip()[:200]}")
    return found[-3:]


def main() -> None:
    log("MONITOR_START interval=%ss url=%s" % (INTERVAL_SEC, HEALTH_URL))
    seen_errors: set[str] = set()
    while True:
        h = health()
        if h:
            log(
                f"HEALTH_OK queue={h.get('queue')} lmstudio={h.get('lmstudio')} "
                f"vram={h.get('vram_mib')}MiB | {db_snapshot()}"
            )
            conn = sqlite3.connect(DB)
            row = conn.execute(
                "SELECT artifact_id FROM artifacts WHERE project_id=9 AND status NOT IN ('done','error') ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.close()
            if row:
                log(f"PIPELINE {row[0]} {checklist_step(row[0])}")
        for err in tail_errors():
            if err not in seen_errors:
                seen_errors.add(err)
                log(f"NEW_ERROR {err}")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
