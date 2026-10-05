"""MCP-сервер карты для ИИ-агентов (stdio, только чтение): поиск и связи по графу проекта без обращения к исходникам.

Запуск: `projectmap mcp` (в каталоге проекта или с `--root`). Граф берётся из конфига; если его нет, строится при первом запросе.
Инструменты: search, symbol, file, callers, callees, neighbors, impact, stats, coverage. Протокол — JSON-RPC по строкам stdin/stdout,
без внешних библиотек.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections import deque
from pathlib import Path
from typing import Any

PROTOCOL = "2024-11-05"
SERVER_NAME = "projectmap"
SEARCH_KINDS = ("file", "symbol", "route", "table", "env", "service", "ui")
CALL_RELATIONS = frozenset({"calls", "calls_ref"})
IMPACT_RELATIONS = frozenset({"calls", "calls_ref", "imports", "inherits", "runs", "renders"})

TOOLS: list[tuple[str, str, dict[str, Any]]] = [
    ("search", "Найти по имени, пути или ключевому слову: файлы, функции, роуты, таблицы, переменные окружения, элементы интерфейса.",
     {"query": {"type": "string"}, "kind": {"type": "string", "description": "file, symbol, route, table, env, service или ui; пусто — любой"},
      "limit": {"type": "integer", "default": 15}}),
    ("symbol", "Где определена функция, метод или класс (по имени или полному имени): файл:строка, сигнатура, описание, число вызовов.",
     {"query": {"type": "string"}}),
    ("file", "Карточка файла: подсистема, метрики (fan-in/out, нестабильность, правки), определения, соседи, роуты, таблицы, тесты.",
     {"query": {"type": "string", "description": "путь или его конец, например services/queue.py"}}),
    ("callers", "Кто вызывает функцию.", {"query": {"type": "string"}, "limit": {"type": "integer", "default": 20}}),
    ("callees", "Что вызывает функция и где это определено.", {"query": {"type": "string"}, "limit": {"type": "integer", "default": 20}}),
    ("neighbors", "Окрестность узла в графе: кто связан с ним на глубине 1–3.",
     {"query": {"type": "string"}, "depth": {"type": "integer", "default": 1}, "direction": {"type": "string", "enum": ["both", "in", "out"], "default": "both"}}),
    ("impact", "Что заденет правка файла: файлы, которые зависят от него напрямую и через цепочку (глубина 1–4), и тесты на него.",
     {"query": {"type": "string"}, "depth": {"type": "integer", "default": 2}}),
    ("stats", "Сводка по карте: узлы и связи по видам, подсистемы, группы связности, хабы, файлы не на своём месте.", {}),
    ("coverage", "Охват карты по слоям: какая доля проекта описана в графе и что пропущено.", {}),
]


class Graph:
    """Граф из json с индексами для запросов; перечитывается, если файл изменился."""

    def __init__(self, root: Path, path: Path) -> None:
        self.root, self.path = root, path
        self._mtime = 0.0
        self.data: dict[str, Any] = {"nodes": [], "edges": []}
        self.reload()

    def reload(self) -> None:
        if not self.path.is_file():
            self._build()
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return
        self._mtime = mtime
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.nodes = {n["id"]: n for n in self.data["nodes"]}
        self.out: dict[str, list[dict[str, Any]]] = {}
        self.inc: dict[str, list[dict[str, Any]]] = {}
        self.owner = {i: i for i, n in self.nodes.items() if n["kind"] == "file"}
        for e in self.data["edges"]:
            self.out.setdefault(e["from"], []).append(e)
            self.inc.setdefault(e["to"], []).append(e)
            if e["rel"] == "defines":
                self.owner[e["to"]] = e["from"]

    def _build(self) -> None:
        from projectmap.cli import build_and_write
        from projectmap.config import load_config

        config = load_config(self.root)
        build_and_write(self.root, config, self.path)

    def label(self, node_id: str) -> str:
        n = self.nodes.get(node_id)
        if not n:
            return node_id
        where = f" — {n['path']}:{n['line']}" if n.get("path") and n.get("line") else f" — {n['path']}" if n.get("path") else ""
        return f"{n['kind']} {n.get('label', node_id)}{where}"

    def find(self, query: str, kinds: tuple[str, ...] = SEARCH_KINDS, limit: int = 15) -> list[dict[str, Any]]:
        q = query.strip().lower()
        if not q:
            return []
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for n in self.nodes.values():
            if n["kind"] not in kinds:
                continue
            label, path = str(n.get("label", "")).lower(), str(n.get("path", "")).lower()
            qual = n["id"].split("::", 1)[-1].lower() if "::" in n["id"] else label
            if q in (label, qual) or (n["kind"] == "file" and path == q):
                score = 0
            elif path.endswith("/" + q) or label.startswith(q) or qual.startswith(q):
                score = 1
            elif q in label or q in path or q in qual:
                score = 2
            elif any(q in str(k).lower() for k in n.get("keywords", [])):
                score = 3
            else:
                continue
            scored.append((score, label, n))
        scored.sort(key=lambda x: (x[0], len(x[1]), x[1]))
        return [n for _, _, n in scored[:limit]]

    def one(self, query: str, kinds: tuple[str, ...] = SEARCH_KINDS) -> dict[str, Any] | None:
        found = self.find(query, kinds, limit=1)
        return found[0] if found else None


def _limit(arguments: dict, default: int) -> int:
    return max(1, min(100, int(arguments.get("limit") or default)))


def _line(g: Graph, node_id: str, extra: str = "") -> str:
    return f"- {g.label(node_id)}{extra}"


def tool_search(g: Graph, a: dict) -> str:
    kinds = (a["kind"],) if a.get("kind") in SEARCH_KINDS else SEARCH_KINDS
    found = g.find(str(a.get("query", "")), kinds, _limit(a, 15))
    return "\n".join(_line(g, n["id"]) for n in found) or "ничего не найдено"


def tool_symbol(g: Graph, a: dict) -> str:
    found = g.find(str(a.get("query", "")), ("symbol",), 8)
    if not found:
        return "символ не найден"
    out = []
    for n in found:
        calls = sum(1 for e in g.inc.get(n["id"], []) if e["rel"] in CALL_RELATIONS)
        head = f"{n.get('label')} ({n.get('sub') or 'символ'}) — {n.get('path')}:{n.get('line')}; вызовов: {calls}"
        for key in ("signature", "doc"):
            if n.get(key):
                head += f"\n    {n[key]}"
        out.append(head)
    return "\n".join(out)


def tool_file(g: Graph, a: dict) -> str:
    n = g.one(str(a.get("query", "")), ("file",))
    if not n:
        return "файл не найден"
    fid = n["id"]
    defined = [g.nodes[e["to"]] for e in g.out.get(fid, []) if e["rel"] == "defines"]
    deps: dict[str, int] = {}
    users: dict[str, int] = {}
    for e in g.data["edges"]:
        if e["rel"] not in IMPACT_RELATIONS:
            continue
        src, dst = g.owner.get(e["from"]), g.owner.get(e["to"])
        if src == fid and dst and dst != fid:
            deps[dst] = deps.get(dst, 0) + 1
        elif dst == fid and src and src != fid:
            users[src] = users.get(src, 0) + 1
    top = lambda d: ", ".join(f"{g.nodes[k]['path']} ({v})" for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:8]) or "—"  # noqa: E731
    lines = [f"{n['path']} — подсистема {n.get('group')}, группа связности {n.get('community')}",
             f"fan-in {n.get('fan_in', 0)}, fan-out {n.get('fan_out', 0)}, нестабильность {n.get('instability')}, правок {n.get('churn', 0)}",
             f"ключевые слова: {', '.join(n.get('keywords', [])) or '—'}",
             f"определяет ({len(defined)}): " + ", ".join(f"{s['label']}:{s['line']}" for s in defined[:15]),
             f"его используют: {top(users)}", f"он использует: {top(deps)}"]
    for rel, title in (("reads", "читает таблицы"), ("writes", "пишет в таблицы"), ("reads_env", "читает переменные")):
        names = sorted({g.nodes[e["to"]]["label"] for s in defined for e in g.out.get(s["id"], []) if e["rel"] == rel and e["to"] in g.nodes})
        if names:
            lines.append(f"{title}: {', '.join(names[:12])}")
    routes = sorted({g.nodes[e["from"]]["label"] for s in defined for e in g.inc.get(s["id"], []) if e["rel"] == "handles"})
    if routes:
        lines.append("роуты: " + ", ".join(routes[:12]))
    tests = sorted({g.nodes[g.owner[e["from"]]]["path"] for s in defined for e in g.inc.get(s["id"], []) if e["rel"] == "covers" and e["from"] in g.owner})
    lines.append("тесты: " + (", ".join(tests[:10]) or "нет"))
    return "\n".join(lines)


def _calls(g: Graph, a: dict, incoming: bool) -> str:
    n = g.one(str(a.get("query", "")), ("symbol",))
    if not n:
        return "функция не найдена"
    edges = [e for e in (g.inc if incoming else g.out).get(n["id"], []) if e["rel"] in CALL_RELATIONS]
    seen, lines = set(), [f"{g.label(n['id'])}: {'вызывают' if incoming else 'вызывает'} {len(edges)}"]
    for e in edges:
        other = e["from"] if incoming else e["to"]
        if other not in seen and len(seen) < _limit(a, 20):
            seen.add(other)
            lines.append(_line(g, other, f" (×{e.get('n', 1)})" if (e.get("n") or 1) > 1 else ""))
    return "\n".join(lines)


def tool_neighbors(g: Graph, a: dict) -> str:
    n = g.one(str(a.get("query", "")))
    if not n:
        return "узел не найден"
    depth, direction = max(1, min(3, int(a.get("depth") or 1))), a.get("direction") if a.get("direction") in ("in", "out") else "both"
    seen, queue, lines = {n["id"]}, deque([(n["id"], 0)]), [f"окрестность {g.label(n['id'])}, глубина {depth}:"]
    while queue and len(lines) < 80:
        node, d = queue.popleft()
        if d == depth:
            continue
        steps = ([(e["to"], "→", e["rel"]) for e in g.out.get(node, [])] if direction != "in" else []) + \
                ([(e["from"], "←", e["rel"]) for e in g.inc.get(node, [])] if direction != "out" else [])
        for other, arrow, rel in steps:
            if other not in seen and rel not in ("defines", "changed", "co_changes"):
                seen.add(other)
                queue.append((other, d + 1))
                lines.append(f"{'  ' * d}{arrow} {rel}: {g.label(other)}")
    return "\n".join(lines)


def tool_impact(g: Graph, a: dict) -> str:
    n = g.one(str(a.get("query", "")), ("file",))
    if not n:
        return "файл не найден"
    depth = max(1, min(4, int(a.get("depth") or 2)))
    users: dict[str, set[str]] = {}
    tests: dict[str, set[str]] = {}
    for e in g.data["edges"]:
        src, dst = g.owner.get(e["from"]), g.owner.get(e["to"])
        if not src or not dst or src == dst:
            continue
        if e["rel"] in IMPACT_RELATIONS:
            users.setdefault(dst, set()).add(src)
        elif e["rel"] == "covers":
            tests.setdefault(dst, set()).add(src)
    levels, seen, frontier = [], {n["id"]}, {n["id"]}
    for _ in range(depth):
        frontier = {s for f in frontier for s in users.get(f, ())} - seen
        if not frontier:
            break
        seen |= frontier
        levels.append(sorted(frontier))
    lines = [f"правка {n['path']} заденет {len(seen) - 1} файлов:"]
    for i, level in enumerate(levels, 1):
        lines.append(f"  глубина {i} ({len(level)}): " + ", ".join(g.nodes[x]["path"] for x in level[:15]) + (" …" if len(level) > 15 else ""))
    covering = sorted({g.nodes[t]["path"] for f in seen for t in tests.get(f, ())})
    lines.append("тесты, которые стоит прогнать: " + (", ".join(covering[:15]) or "не найдено"))
    return "\n".join(lines)


def tool_stats(g: Graph, a: dict) -> str:
    stats, metrics = g.data.get("stats") or {}, g.data.get("metrics") or {}
    lines = [f"узлов {stats.get('nodes')}, связей {stats.get('edges')}", "узлы: " + ", ".join(f"{k} {v}" for k, v in (stats.get("by_kind") or {}).items()),
             "подсистемы: " + ", ".join(f"{k} {v}" for k, v in list((stats.get("by_group") or {}).items())[:12])]
    if metrics:
        lines.append(f"групп связности {len(metrics.get('communities', []))}, модулярность {metrics.get('modularity')}, согласие с подсистемами {metrics.get('agreement_with_groups')}")
        lines.append("хабы: " + ", ".join(h["path"] for h in metrics.get("hubs", [])[:8]))
        lines.append("не на своём месте: " + ", ".join(m["path"] for m in metrics.get("misplaced", [])[:8]))
    return "\n".join(lines)


def tool_coverage(g: Graph, a: dict) -> str:
    from projectmap.config import load_config
    from projectmap.context import ProjectContext
    from projectmap.coverage import render_text
    from projectmap.probes import run_probes

    return render_text(run_probes(ProjectContext(g.root, g.data, load_config(g.root))))


HANDLERS = {"search": tool_search, "symbol": tool_symbol, "file": tool_file, "callers": lambda g, a: _calls(g, a, True),
            "callees": lambda g, a: _calls(g, a, False), "neighbors": tool_neighbors, "impact": tool_impact, "stats": tool_stats, "coverage": tool_coverage}


def tool_schemas() -> list[dict[str, Any]]:
    return [{"name": name, "description": about, "inputSchema": {"type": "object", "properties": props, "required": ["query"] if "query" in props else []}}
            for name, about, props in TOOLS]


def handle(message: dict, graph: Graph) -> dict | None:
    method, mid, params = message.get("method"), message.get("id"), message.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "1.0"},
            "instructions": "Карта проекта: сначала search или file, для связей — callers/callees/neighbors, перед правкой — impact. "
                            "Ответ — указатель; проверяйте найденное в самом файле."}}
    if (method or "").startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_schemas()}}
    if method == "tools/call":
        try:
            name = str(params.get("name"))
            if name not in HANDLERS:
                raise ValueError(f"неизвестный инструмент {name!r}")
            graph.reload()
            text = HANDLERS[name](graph, params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except Exception as exc:  # noqa: BLE001
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"ошибка: {exc}"}], "isError": True}}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"метод не поддерживается: {method}"}}


def serve(root: Path, graph_path: Path) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    graph = Graph(root, graph_path)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(message, graph)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


def client_config(root: Path) -> dict[str, Any]:
    """Фрагмент `.mcp.json` для подключения сервера к ИИ-клиенту (Claude Code, Cursor)."""
    server: dict[str, Any] = {"command": Path(sys.executable).as_posix(), "args": ["-m", "projectmap", "mcp", "--root", root.resolve().as_posix()]}
    package_parent = Path(__file__).resolve().parents[1]
    if "site-packages" not in package_parent.parts:  # запуск из исходников, пакет не установлен
        server["env"] = {"PYTHONPATH": package_parent.as_posix()}
    return {"mcpServers": {SERVER_NAME: server}}
