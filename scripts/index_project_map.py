"""Индексатор проекта (карта, код Python/JS/шаблонов/CSS, документы, символы, роуты, БД, окружение, история).

  venv\\Scripts\\python.exe scripts\\index_project_map.py            # инкрементально: только изменённые файлы
  venv\\Scripts\\python.exe scripts\\index_project_map.py --full     # пересобрать всё (кэш эмбеддингов сохраняется)
  venv\\Scripts\\python.exe scripts\\index_project_map.py --changed  # то же, что без флагов (для хуков и restart.ps1)
  venv\\Scripts\\python.exe scripts\\index_project_map.py --no-embed # только структура и точный поиск, без Ollama
  venv\\Scripts\\python.exe scripts\\index_project_map.py --status   # что устарело, не меняя индекс

Хранилище: data/project_map.db. Лог: data/logs/index.log. Эмбеддинги — Ollama bge-m3 на CPU, GPU не занимается.
Настройки: scripts/index_config.yaml, словарь синонимов: scripts/index_glossary.yaml.
Подробности: docs/plans/PROJECT-INDEX-PLAN.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--full", action="store_true", help="пересобрать всё")
    parser.add_argument("--changed", action="store_true", help="только изменённые файлы (по умолчанию)")
    parser.add_argument("--no-embed", action="store_true", help="не считать эмбеддинги")
    parser.add_argument("--status", action="store_true", help="показать, что устарело")
    parser.add_argument("--only", nargs="*", help="индексировать только эти файлы")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.status:
        from app.app.services.project_index import search, store

        conn = store.connect()
        info = search.staleness(conn)
        print(json.dumps({k: (v[:20] if isinstance(v, list) else v) for k, v in info.items()}, ensure_ascii=False, indent=1))
        return 0 if not info["changed"] and not info["removed"] else 3

    from app.app.services.project_index.indexer import run_index

    summary = run_index(full=args.full, embed=not args.no_embed, echo=not args.quiet, only=args.only)
    return 0 if summary["embed_pending"] == 0 or args.no_embed else 4


if __name__ == "__main__":
    raise SystemExit(main())
