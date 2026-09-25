"""Диагностика индекса проекта: всё ли в порядке и что чинить. Только чтение.

  venv\\Scripts\\python.exe scripts\\index_doctor.py          # отчёт, код выхода 1 при проблемах
  venv\\Scripts\\python.exe scripts\\index_doctor.py --json   # то же в JSON (для мониторинга)

Проверяет: sqlite-vec и FTS5, версию схемы, Ollama и модель bge-m3, размер и состав индекса, возраст,
устаревшие файлы, фрагменты без векторов, блокировку, результат последнего замера качества.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

from app.app.services.project_index import search, store  # noqa: E402
from app.app.services.project_index.config import CHUNKER_VERSION, DB_PATH, LOCK_PATH, SCHEMA_VERSION  # noqa: E402


def check() -> tuple[list[dict], bool]:
    rows: list[dict] = []
    bad = False

    def add(name: str, ok: bool | None, detail: str, hint: str = "") -> None:
        nonlocal bad
        rows.append({"check": name, "status": "OK" if ok else ("WARN" if ok is None else "FAIL"), "detail": detail, "hint": hint})
        if ok is False:
            bad = True

    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        add("FTS5", True, f"SQLite {sqlite3.sqlite_version}")
    except sqlite3.Error as exc:
        add("FTS5", False, str(exc), "нужна сборка SQLite с FTS5")
    try:
        c = sqlite3.connect(":memory:")
        store.load_vec_ext(c)
        add("sqlite-vec", True, c.execute("select vec_version()").fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        add("sqlite-vec", False, repr(exc), "pip install sqlite-vec")
    try:
        import httpx
        from app.app.config import Settings

        s = Settings()
        tags = httpx.get(f"{s.ollama_base_url.rstrip('/')}/api/tags", timeout=5).json()
        models = [m["name"] for m in tags.get("models", [])]
        has = any(m.lower().startswith(s.ollama_embed_model.lower()) for m in models)
        add("Ollama и модель эмбеддингов", has, f"{s.ollama_embed_model}: {'есть' if has else 'нет'}",
            "" if has else f"ollama pull {s.ollama_embed_model}")
    except Exception as exc:  # noqa: BLE001
        add("Ollama и модель эмбеддингов", None, f"Ollama недоступна ({exc.__class__.__name__}): смысловой поиск и досчёт векторов не работают, точный — да",
            "запустите Ollama")
    if not DB_PATH.is_file():
        add("индекс", False, f"нет файла {DB_PATH}", "venv\\Scripts\\python.exe scripts\\index_project_map.py")
        return rows, True
    conn = store.connect()
    ver = store.get_meta(conn, "schema_version")
    add("версия схемы", ver == str(SCHEMA_VERSION), f"{ver} (ожидается {SCHEMA_VERSION})")
    add("версия нарезчика", store.get_meta(conn, "chunker_version") == CHUNKER_VERSION,
        f"{store.get_meta(conn, 'chunker_version')} (ожидается {CHUNKER_VERSION})", "будет пересобран при следующем запуске индексатора")
    size_mb = DB_PATH.stat().st_size / 1e6
    kinds = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM chunks GROUP BY kind ORDER BY 2 DESC")}
    add("состав индекса", sum(kinds.values()) > 0, f"{sum(kinds.values())} фрагментов, {size_mb:.1f} МБ: " + ", ".join(f"{k} {v}" for k, v in list(kinds.items())[:8]))
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("symbols", "calls", "routes", "db_columns", "env_vars", "ui_links", "test_cover", "history")}
    add("производные таблицы", all(counts[t] > 0 for t in ("symbols", "calls", "routes", "db_columns")), ", ".join(f"{k} {v}" for k, v in counts.items()))
    st = search.staleness(conn)
    n_stale = len(st["changed"]) + len(st["removed"])
    add("свежесть", n_stale == 0, f"изменено файлов {len(st['changed'])}, удалено {len(st['removed'])}; возраст {st['age_hours']} ч",
        "" if n_stale == 0 else "venv\\Scripts\\python.exe scripts\\index_project_map.py")
    add("векторы", st["embed_pending"] == 0, f"без вектора: {st['embed_pending']}",
        "" if st["embed_pending"] == 0 else "запустите индексатор с работающей Ollama")
    if LOCK_PATH.is_file():
        add("блокировка", None, f"файл {LOCK_PATH.name} существует (индексация идёт или процесс упал; устаревшая снимется сама)")
    all_evals = sorted((ROOT / "data" / "eval").glob("index_eval_*.json"))
    dev = [p for p in all_evals if "holdout" not in p.name]
    hold = [p for p in all_evals if "holdout" in p.name]
    if dev:
        data = json.loads(dev[-1].read_text(encoding="utf-8"))["summary"]["total"]
        add("качество, набор для настройки", data["recall5"] >= 0.85,
            f"recall@5 {data['recall5']:.0%}, recall@1 {data['recall1']:.0%}, MRR {data['mrr']:.2f} ({dev[-1].name})",
            "venv\Scripts\python.exe scripts\eval_index.py --show-misses")
    else:
        add("качество, набор для настройки", None, "замеров нет", "venv\Scripts\python.exe scripts\eval_index.py")
    if hold:
        data = json.loads(hold[-1].read_text(encoding="utf-8"))["summary"]["total"]
        add("качество, контрольный набор", None if data["recall5"] < 0.85 else True,
            f"recall@5 {data['recall5']:.0%}, recall@10 {data['recall10']:.0%}, MRR {data['mrr']:.2f} — веса на нём не подбирались ({hold[-1].name})")
    log = ROOT / "data" / "logs" / "index.log"
    if log.is_file():
        add("лог", True, f"{log.name}: последнее обновление {time.strftime('%Y-%m-%d %H:%M', time.localtime(log.stat().st_mtime))}")
    conn.close()
    return rows, bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows, bad = check()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        for r in rows:
            mark = {"OK": "✓", "WARN": "!", "FAIL": "✗"}[r["status"]]
            print(f"{mark} {r['check']}: {r['detail']}")
            if r["hint"] and r["status"] != "OK":
                print(f"    → {r['hint']}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
