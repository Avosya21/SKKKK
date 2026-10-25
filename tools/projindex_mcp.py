"""Локальный MCP-сервер индекса проекта (stdio): поиск по коду, связям, БД, окружению и истории. Только чтение.

Отдельно от проектного MCP портала (routers/project_mcp.py): тот отдаёт данные проектов внешним агентам по ключам,
а этот — исходный код и служебную карту, поэтому наружу не публикуется и работает только локально через stdio.

Подключение (Claude Code): файл .mcp.json в корне репозитория уже описывает сервер `primaryanalitics-index`.
Вручную:  venv\\Scripts\\python.exe tools\\projindex_mcp.py

Инструменты: search_project, find_symbol, callers, callees, route, table, env, ui_element, tests_for, ops,
history, changed, invariants, why_changed, graph_neighbors, module_deps, orphans, import_cycles.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROTOCOL = "2024-11-05"

TOOLS = [
    ("search_project", "search", "Вопрос по смыслу или имя: гибридный поиск по коду, шаблонам, JS, документам и решениям. Ответ — файл:строка и фрагмент."),
    ("find_symbol", "symbol", "Где определена функция, метод, класс или константа модуля (сигнатура, докстринг). "
                                 "Принимает и название функции словами: memory_build, search_map."),
    ("callers", "callers", "Кто вызывает функцию (включая передачу как значение: Depends, to_thread)."),
    ("callees", "callees", "Что вызывает функция и где это определено."),
    ("route", "route", "HTTP-роут: обработчик, зависимости, проверки прав, вызываемые функции, таблицы. Например `GET /projects/1/artifacts`."),
    ("table", "table", "Таблица БД: столбцы, миграции, кто пишет и читает."),
    ("env", "env", "Переменная окружения или константа модуля: значение по умолчанию, где читается, описана ли в .env.example."),
    ("ui_element", "ui", "Кнопка или текст интерфейса по подписи или id: шаблон, JS-обработчик, вызываемый API."),
    ("tests_for", "tests", "Какие тесты покрывают модуль или функцию и как их запустить."),
    ("ops", "ops", "Запуск и эксплуатация: порты, логи, каталоги данных, скрипты."),
    ("history", "history", "Коммиты по файлу или теме."),
    ("changed", "changed", "Незакоммиченные правки, сопоставленные с функциями."),
    ("invariants", "invariants", "Что нельзя ломать: инварианты карты и решения DECISIONS."),
    ("why_changed", "why_changed", "Последние изменения функции (git log -L)."),
    ("graph_neighbors", "graph", "Граф зависимостей: окрестность сущности — что она вызывает и что вызывает её "
                                 "(функция, файл, роут, таблица, кнопка). Параметры: depth 1–4, direction both|in|out."),
    ("module_deps", "deps", "Зависимости модуля: импорты, вызовы, таблицы БД, роуты файла."),
    ("orphans", "orphans", "Функции, которых никто не вызывает и на которые нет тестов (кандидаты в мёртвый код)."),
    ("import_cycles", "cycles", "Циклы импортов между файлами проекта."),
]

GRAPH_MODES = {"graph", "deps", "orphans", "cycles"}


def _tool_schema(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "вопрос или имя (для `changed` можно пустой)"},
                "limit": {"type": "integer", "description": "сколько результатов (для search_project), 1-30", "default": 8},
                "depth": {"type": "integer", "description": "глубина окрестности для graph_neighbors, 1-4", "default": 1},
                "direction": {"type": "string", "description": "направление связей для graph_neighbors: both, in, out",
                              "enum": ["both", "in", "out"], "default": "both"},
            },
            "required": [] if name in {"changed", "orphans", "import_cycles"} else ["query"],
        },
    }


def call_tool(name: str, arguments: dict) -> str:
    from app.app.services.project_index import search, store

    mode = next((m for n, m, _ in TOOLS if n == name), None)
    if mode is None:
        raise ValueError(f"неизвестный инструмент {name!r}")
    query = str(arguments.get("query", ""))[:300]
    limit = max(1, min(30, int(arguments.get("limit", 8) or 8)))
    if mode in GRAPH_MODES:
        from app.app.services.project_index import graph as G
        from app.app.services.project_index import graph_atlas as A

        conn = store.connect()
        try:
            if mode == "orphans":
                return A.orphans_text(G.orphans(conn, limit=limit if limit > 8 else 40),
                                      total=len(G.orphans(conn, limit=10_000)))
            if mode == "cycles":
                return A.cycles_text(G.import_cycles(conn, limit=max(limit, 10)))
            built = G.build(conn)
        finally:
            conn.close()
        if mode == "deps":
            return A.module_deps_text(built, query, limit=max(limit, 20))
        depth = max(1, min(4, int(arguments.get("depth", 1) or 1)))
        direction = str(arguments.get("direction", "both"))
        if direction not in {"both", "in", "out"}:
            direction = "both"
        return A.neighbors_text(G.neighbors(built, query, depth=depth, direction=direction, limit=400),
                                limit=max(limit, 30))
    conn = store.connect()
    try:
        result = search.unified(conn, mode, query, limit)
        stale = search.staleness(conn)
    finally:
        conn.close()
    text = search.as_text(result)
    if stale["changed"] or stale["removed"]:
        text += f"\n\n⚠ индекс отстаёт от кода: изменено файлов {len(stale['changed'])}, удалено {len(stale['removed'])}. " \
                "Обновите: venv\\Scripts\\python.exe scripts\\index_project_map.py"
    if stale["embed_pending"]:
        text += f"\n⚠ у {stale['embed_pending']} фрагментов нет вектора (смысловой поиск неполный)."
    return text


def handle(message: dict) -> dict | None:
    method = message.get("method")
    mid = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "primaryanalitics-index", "version": "1.0"},
            "instructions": "Индекс проекта PrimaryAnalitics. Сначала search_project или find_symbol; для связей — callers/callees/route/table; "
                            "перед правкой кода проверяйте результат в самом файле.",
        }}
    if method in {"notifications/initialized", "notifications/cancelled"} or (method or "").startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [_tool_schema(n, d) for n, _, d in TOOLS]}}
    if method == "tools/call":
        try:
            text = call_tool(str(params.get("name")), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except Exception as exc:  # noqa: BLE001
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"ошибка: {exc}"}], "isError": True}}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"метод не поддерживается: {method}"}}


def main() -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
