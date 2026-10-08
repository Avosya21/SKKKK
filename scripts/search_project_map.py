"""Поиск по индексу проекта: смысл + точные имена + связи. Каждый ответ — с адресом `путь:строка`.

Вопрос простыми словами (гибрид: точный + смысловой):
  venv\\Scripts\\python.exe scripts\\search_project_map.py "почему исчерпывается пул соединений"

Режимы (точные, по таблицам индекса):
  --symbol NAME          где определён символ (функция, метод, класс), сигнатура, докстринг;
                        NAME может быть названием функции словами: memory_build, search_map
  --callers NAME         кто вызывает (включая передачу как значение: Depends, to_thread)
  --callees NAME         что вызывает эта функция и где определено
  --imports PATH         что импортирует файл;  --imported-by MODULE  кто его импортирует
  --route "GET /url"     обработчик, зависимости, проверки прав, вызываемые функции, таблицы; --roles admin
  --endpoint URL         кто на фронте ходит по этому URL и какой роут отвечает
  --table NAME           столбцы, миграции, кто пишет и читает таблицу
  --env NAME             переменная окружения: значение по умолчанию, где читается, описана ли в .env.example
  --ui "текст"           кнопка/элемент по подписи или id → шаблон, JS-обработчик, API; также строки интерфейса
  --tests TARGET         какие тесты покрывают модуль или символ и как их запустить
  --ops "вопрос"         порты, логи, каталоги, скрипты, запуск
  --history [PATH]       коммиты по файлу или теме (--since 7d);  --changed  — незакоммиченные правки по функциям
  --why-changed NAME     последние изменения функции (git log -L)
  --invariants [ТЕКСТ]   что нельзя ломать (§13 карты и решения DECISIONS)

Граф зависимостей (все сущности проекта и связи между ними):
  --graph УЗЕЛ           окрестность сущности: функция, файл, роут, таблица, кнопка (--depth 1–4, --direction in|out)
  --deps ФАЙЛ            зависимости модуля: импорты, вызовы, таблицы БД, роуты
  --orphans              функции, которых никто не вызывает и на которые нет тестов
  --import-cycles        циклы импортов между файлами проекта

Общие флаги: --limit N, --json, --kind K, --path ПОДСТРОКА, --doc-only, --code-only, --context N,
--no-notes, --auto (дообновить устаревшие файлы перед поиском), --no-vector (только точный).
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

from app.app.services.project_index import search as S  # noqa: E402
from app.app.services.project_index import store  # noqa: E402
from app.app.services.project_index.config import redact  # noqa: E402


def _snippet(text: str, context: int) -> str:
    lines = text.splitlines()
    body = lines[1:] if lines and ("::" in lines[0] or lines[0].split("  ")[0].count(":") >= 1 and "/" in lines[0]) else lines
    body = [l.rstrip() for l in body if l.strip()][: max(3, context)]
    return "\n".join("    " + l[:160] for l in body)


def print_hits(result: dict, context: int) -> None:
    for note in result.get("notes", []):
        print(f"⚠ {note}")
    hits = result["hits"]
    if not hits:
        print("ничего не найдено")
        return
    for i, h in enumerate(hits, 1):
        loc = f"{h['path']}:{h['line_start']}" + (f"-{h['line_end']}" if h["line_end"] and h["line_end"] != h["line_start"] else "")
        print(f"\n[{i}] {loc}  {h['symbol'] or h['title']}  ({h['kind']}, {h['score']}, {'+'.join(h['sources'])})")
        print(_snippet(h["text"], context))
        nb = h.get("neighbors") or {}
        if nb.get("prev") or nb.get("next"):
            print(f"    ↑ {nb.get('prev') or '—'}   ↓ {nb.get('next') or '—'}")
        for rel in h.get("related", []):
            print(f"    ⓘ {rel['path']}:{rel['line']}  {rel['title']}")


def _fmt_symbols(rows: list[dict]) -> None:
    if not rows:
        print("символ не найден")
    for r in rows:
        print(f"{r['path']}:{r['line']}-{r['end_line']}  {r['qualname']} [{r['kind']}]  {r['signature'][:110]}")
        if r.get("matched_alias"):
            print(f"    найдено по названию функции: {r['matched_alias']} ({r.get('matched_source')})")
        if r["decorators"]:
            print(f"    декораторы: {r['decorators'][:120]}")
        if r["doc"]:
            print(f"    {r['doc'][:160]}")


def _graph_limit(limit: int) -> int:
    """Для видов графа `--limit` по умолчанию (8) мал: берём осмысленный размер окрестности."""
    return limit if limit > 8 else 300


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?", default="", help="вопрос")
    ap.add_argument("--limit", type=int, default=S.DEFAULT_LIMIT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--kind", action="append")
    ap.add_argument("--path")
    ap.add_argument("--doc-only", action="store_true")
    ap.add_argument("--code-only", action="store_true")
    ap.add_argument("--context", type=int, default=10)
    ap.add_argument("--no-notes", action="store_true")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--no-vector", action="store_true")
    ap.add_argument("--rerank", action="store_true", help="переранжировать верх выдачи локальной LLM (нужен LM Studio, +несколько секунд)")
    for name in ("symbol", "callers", "callees", "imports", "imported-by", "route", "endpoint", "table", "env", "ui", "tests",
                 "ops", "why-changed", "roles", "since"):
        ap.add_argument(f"--{name}")
    ap.add_argument("--history", nargs="?", const="")
    ap.add_argument("--invariants", nargs="?", const="")
    ap.add_argument("--changed", action="store_true")
    ap.add_argument("--graph", metavar="УЗЕЛ", help="окрестность сущности в графе проекта (функция, файл, роут, таблица)")
    ap.add_argument("--deps", metavar="ФАЙЛ", help="зависимости модуля: импорты, вызовы, таблицы, роуты")
    ap.add_argument("--orphans", action="store_true", help="функции без вызовов и без тестов")
    ap.add_argument("--import-cycles", dest="import_cycles", action="store_true", help="циклы импортов между файлами")
    ap.add_argument("--depth", type=int, default=1, help="глубина окрестности для --graph (1–4)")
    ap.add_argument("--direction", choices=("both", "in", "out"), default="both", help="направление связей для --graph")
    a = ap.parse_args()

    try:
        conn = store.connect()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2

    stale = S.staleness(conn)
    if (stale["changed"] or stale["removed"]) and a.auto:
        conn.close()
        from app.app.services.project_index.indexer import run_index

        run_index(echo=False)
        conn = store.connect()
        stale = S.staleness(conn)
    if not a.json and (stale["changed"] or stale["removed"] or stale["embed_pending"]):
        print(f"⚠ индекс отстаёт: изменено файлов {len(stale['changed'])}, удалено {len(stale['removed'])}, "
              f"без вектора {stale['embed_pending']} — запустите scripts/index_project_map.py (или --auto)", file=sys.stderr)

    out: object
    if a.symbol:
        out = S.symbol_info(conn, a.symbol)
        if not a.json:
            _fmt_symbols(out)  # type: ignore[arg-type]
    elif a.callers:
        out = S.callers_of(conn, a.callers)
        if not a.json:
            print(f"вызовы {a.callers}: {len(out)}")  # type: ignore[arg-type]
            for r in out:  # type: ignore[union-attr]
                print(f"  {r['path']}:{r['line']}  в {r['caller']}" + ("  (передана как значение)" if r["ref"] else (f"  [{r['qualifier']}.]" if r["qualifier"] else "")))
    elif a.callees:
        out = S.callees_of(conn, a.callees)
        if not a.json:
            for r in out:  # type: ignore[union-attr]
                print(f"  {r['in']}: {r['callee']}" + (f" → {r['defined']}" if r["defined"] else "  (внешний)") + (f"  ×{r['count']}" if r["count"] > 1 else ""))
    elif a.imports:
        out = S.imports_info(conn, a.imports, False)
        if not a.json:
            for r in out:  # type: ignore[union-attr]
                print(f"  {r['path']}:{r['line']}  {r['module']}  {r['names'] or ''}")
    elif getattr(a, "imported_by"):
        out = S.imports_info(conn, getattr(a, "imported_by"), True)
        if not a.json:
            for r in out:  # type: ignore[union-attr]
                print(f"  {r['path']}:{r['line']}  {r['module']}  {r['names'] or ''}")
    elif a.route is not None or a.roles:
        out = S.routes_info(conn, a.route or "", a.roles)
        if not a.json:
            for r in out:  # type: ignore[union-attr]
                print(f"{r['method']} {r['url']}  → {r['path']}:{r['line']} {r['handler']}")
                print(f"    зависимости: {r['deps'] or '—'}; проверки прав: {r['guards'] or '—'}")
                if r["callees"]:
                    print(f"    вызывает: {', '.join(r['callees'])}")
                if r["tables"]:
                    print(f"    таблицы: {', '.join(r['tables'])}")
    elif a.endpoint:
        out = S.endpoint_info(conn, a.endpoint)
        if not a.json:
            for r in out["routes"]:  # type: ignore[index]
                print(f"роут: {r['method']} {r['url']} → {r['path']}:{r['line']} {r['handler']}")
            for r in out["frontend"]:  # type: ignore[index]
                print(f"фронт: {r['path']}:{r['line']} {r['symbol']}")
    elif a.table:
        out = S.table_info(conn, a.table)
        if not a.json:
            print(f"таблица {out['table']}")  # type: ignore[index]
            for c in out["columns"]:  # type: ignore[index]
                print(f"  {c['col']:<24} {c['ctype'] or '':<28} [{c['source']}] {c['path']}:{c['line']}")
            for c in out.get("access", []):  # type: ignore[union-attr]
                print(f"  {'ПИШЕТ ' if c['writes'] else 'читает'} {c['path']}:{c['line']} {c['symbol']}")
            if out.get("similar"):  # type: ignore[union-attr]
                print("похожие:", ", ".join(out["similar"]))  # type: ignore[index]
    elif a.env:
        out = S.env_info(conn, a.env)
        if not a.json:
            for r in out:  # type: ignore[union-attr]
                doc = "" if r["documented"] is None else ("  [в .env.example]" if r["documented"] else "  [НЕ описана в .env.example]")
                print(f"  {r['name']} — {r['how']}: {r['path']}:{r['line']}  {r['default_value'] or ''}{doc}")
    elif a.ui:
        out = S.ui_info(conn, a.ui)
        if not a.json:
            for block in out:  # type: ignore[union-attr]
                for r in block["elements"]:
                    js = f"  JS {r['js_path']}:{r['js_line']} {r['js_symbol']}" if r["js_path"] else "  (JS-обработчик по id не найден)"
                    print(f"#{r['element_id']} <{r['tag']}> «{r['label']}»  {r['path']}:{r['line']}\n{js}  API: {r['endpoints'] or '—'}")
                for s in block["strings"]:
                    print(f"  текст «{s['text'][:80]}» — {s['path']}:{s['line']}")
    elif a.tests:
        out = S.tests_info(conn, a.tests)
        if not a.json:
            for f in out["files"]:  # type: ignore[index]
                print(f"  {f['path']}  ({f['count']} тестов): {', '.join(f['tests'][:4])}")
            print("запуск:", out["run"] or "тесты не найдены")  # type: ignore[index]
    elif a.ops:
        out = S.ops_info(conn, a.ops, a.limit)
        if not a.json:
            print_hits(out, a.context)  # type: ignore[arg-type]
    elif a.history is not None:
        out = S.history_info(conn, a.history, a.since)
        if not a.json:
            for r in out:  # type: ignore[union-attr]
                print(f"  {r['hash']} {r['date']} {r['author']}: {r['subject']}")
                for f in r["files"][:5]:
                    print(f"      {f}")
    elif a.changed:
        from app.app.services.project_index import analyzers

        out = analyzers.changed_symbols(conn)
        if not a.json:
            for r in out:  # type: ignore[union-attr]
                print(f"  {r['status']:<3} {r['path']}" + (f"  → {', '.join(r['symbols'])}" if r.get("symbols") else ""))
    elif a.why_changed:
        out = S.why_changed(conn, a.why_changed)
        if not a.json:
            print(f"{out.get('symbol')} ({out.get('path')}:{out.get('line')})")  # type: ignore[union-attr]
            for line in out.get("log", []):  # type: ignore[union-attr]
                print("  ", line)
    elif a.invariants is not None:
        out = S.invariants_info(conn, a.invariants)
        if not a.json:
            for r in out:  # type: ignore[union-attr]
                print(f"  • {redact(r['text'])[:200]}\n      {r['source']}:{r['line']}" + (f"  стражи: {r['guard']}" if r["guard"] else ""))
    elif a.graph:
        from app.app.services.project_index import graph as G
        from app.app.services.project_index import graph_atlas as A

        result = G.neighbors(G.build(conn), a.graph, depth=a.depth, direction=a.direction, limit=_graph_limit(a.limit))
        out = result
        if not a.json:
            print(A.neighbors_text(result, limit=min(_graph_limit(a.limit), 60)))
    elif a.deps:
        from app.app.services.project_index import graph as G
        from app.app.services.project_index import graph_atlas as A

        out = A.module_deps_text(G.build(conn), a.deps, limit=min(_graph_limit(a.limit), 40))
        if not a.json:
            print(out)
    elif a.orphans:
        from app.app.services.project_index import graph as G
        from app.app.services.project_index import graph_atlas as A

        total = len(G.orphans(conn, limit=10_000))
        out = G.orphans(conn, limit=min(_graph_limit(a.limit), 100))
        if not a.json:
            print(A.orphans_text(out, total=total))
    elif a.import_cycles:
        from app.app.services.project_index import graph as G
        from app.app.services.project_index import graph_atlas as A

        out = G.import_cycles(conn, limit=20)
        if not a.json:
            print(A.cycles_text(out))
    else:
        if not a.query:
            ap.print_help()
            return 2
        out = S.search(a.query, limit=a.limit, kinds=set(a.kind) if a.kind else None, path_filter=a.path,
                       doc_only=a.doc_only, code_only=a.code_only, conn=conn, use_vectors=not a.no_vector, notes=not a.no_notes, rerank=a.rerank)
        if not a.json:
            print_hits(out, a.context)  # type: ignore[arg-type]
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
