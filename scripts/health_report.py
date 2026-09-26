"""Отчёт о здоровье системы (пункт плана 5.2). Только чтение логов и БД.

    venv\\Scripts\\python scripts\\health_report.py [--json]

Строки логов без даты, поэтому окно — «с последнего запуска процесса» (маркеры `api.started` / `worker.started`).
Показывает: ошибки и предупреждения, отказы входа (throttled), задания GPU, метрики LLM (число вызовов, токены,
ответы без текста), JS-ошибки клиентов, очередь и незавершённые фоновые задачи. Код выхода 1 при красных признаках.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "data" / "logs"
LINE = re.compile(r"^(\d\d:\d\d:\d\d)\s+(\w+)\s+(\S+)\s+(.*)$")


def since_last_start(path: Path, marker: str) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if marker in lines[i]:
            return lines[i:]
    return lines[-5000:]


def analyze(api: list[str], worker: list[str]) -> dict:
    levels: Counter = Counter()
    problems: Counter = Counter()
    llm_calls = llm_tokens = llm_empty = 0
    for line in api + worker:
        m = LINE.match(line)
        if not m:
            continue
        _t, level, logger, msg = m.groups()
        levels[level] += 1
        if level in ("ERROR", "CRITICAL"):
            problems[(logger + " " + msg)[:110]] += 1
        elif level == "WARNING":
            problems["warn: " + (logger + " " + msg)[:100]] += 1
        if "lmstudio.completions_raw ok" in msg or "lmstudio.chat ok" in msg:
            llm_calls += 1
            tokens = re.search(r"completion_tokens=(\d+)", msg)
            llm_tokens += int(tokens.group(1)) if tokens else 0
            chars = re.search(r"chars=(\d+)", msg)
            llm_empty += bool(chars and int(chars.group(1)) == 0)
    text = "\n".join(api + worker)
    return {
        "lines": len(api) + len(worker),
        "levels": dict(levels),
        "top_problems": problems.most_common(8),
        "login_throttled": text.count("login.throttled"),
        "login_denied": text.count("login.denied"),
        "jobs_failed": text.count("job.failed"),
        "jobs_done": text.count("job.done"),
        "purge_retry_failed": text.count("meeting.purge.retry_failed"),
        "client_js_errors": text.count("client.js_error"),
        "llm_calls": llm_calls,
        "llm_completion_tokens": llm_tokens,
        "llm_empty_answers": llm_empty,
        "no_think_selfcheck_failed": "no_think_selfcheck ok=False" in text,
    }


def db_state() -> dict:
    db = ROOT / "data" / "app.db"
    if not db.is_file():
        return {}
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    q = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    out = {
        "jobs_queued": q("SELECT count(*) FROM jobs WHERE status='queued'"),
        "jobs_running": q("SELECT count(*) FROM jobs WHERE status='running'"),
        "jobs_failed_total": q("SELECT count(*) FROM jobs WHERE status='failed'"),
        "artifacts_error": q("SELECT count(*) FROM artifacts WHERE status='error'"),
        "meetings_live": q("SELECT count(*) FROM meetings WHERE status='live'"),
        "tasks_running": q("SELECT count(*) FROM project_tasks WHERE status='running'"),
    }
    con.close()
    return out


def red_flags(a: dict, d: dict) -> list[str]:
    flags = []
    if a["levels"].get("ERROR", 0):
        flags.append(f"ошибок в логах: {a['levels']['ERROR']}")
    if a["no_think_selfcheck_failed"]:
        flags.append("самопроверка обхода reasoning провалена")
    if a["llm_empty_answers"]:
        flags.append(f"пустых ответов LLM: {a['llm_empty_answers']}")
    if a["login_throttled"]:
        flags.append(f"входов заблокировано ограничителем: {a['login_throttled']} (возможен перебор)")
    if a["client_js_errors"]:
        flags.append(f"JS-ошибок у клиентов: {a['client_js_errors']}")
    if a["purge_retry_failed"]:
        flags.append("не удалось стереть записи удалённой встречи (файлы заняты процессом записи)")
    if d.get("artifacts_error"):
        flags.append(f"артефактов в статусе error: {d['artifacts_error']}")
    return flags


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    a = analyze(since_last_start(LOGS / "api.log", "api.started"), since_last_start(LOGS / "worker.log", "worker.started"))
    d = db_state()
    flags = red_flags(a, d)
    if args.json:
        print(json.dumps({"logs": a, "db": d, "red_flags": flags}, ensure_ascii=False, indent=2))
    else:
        print(f"Логи с последнего запуска: {a['lines']} строк, уровни {a['levels']}")
        print(f"Вход: заблокировано {a['login_throttled']}, отказов {a['login_denied']}")
        print(f"GPU-задания: выполнено {a['jobs_done']}, упало {a['jobs_failed']}")
        print(f"LLM: вызовов {a['llm_calls']}, токенов {a['llm_completion_tokens']}, пустых {a['llm_empty_answers']}")
        print(f"Очередь: {d}")
        for text, n in a["top_problems"]:
            print(f"  x{n} {text}")
        print("КРАСНЫЕ ПРИЗНАКИ:" if flags else "Красных признаков нет.")
        for f in flags:
            print(" -", f)
    return 1 if flags else 0


if __name__ == "__main__":
    sys.exit(main())
