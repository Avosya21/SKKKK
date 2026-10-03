"""Атлас зависимостей и текстовые виды графа проекта: markdown для человека, строки для CLI и MCP.

`atlas()` собирает `docs/project-map/DEPENDENCIES.md` между маркерами `generated:begin atlas` / `end atlas`
(рукописная шапка файла сохраняется), остальные функции дают компактный текст для терминала и инструментов.
"""

from __future__ import annotations

import collections
import time
from typing import Any

from app.app.services.project_index import graph as G

ATLAS_PATH = G.ROOT / "docs" / "project-map" / "DEPENDENCIES.md"
BEGIN = "<!-- generated:begin atlas -->"
END = "<!-- generated:end atlas -->"
ATLAS_INTRO = """# Атлас зависимостей SKKKK

Сгенерированная карта проекта: все сущности и все связи между ними. Источник — индекс `data/project_map.db`
и семантический манифест `docs/project-map/manifest.yaml`; полный граф — `data/project_map_graph.json`,
свод по модулям — `docs/project-map/graph-modules.json`.

**Обновить после правок кода:**

```powershell
venv\\Scripts\\python.exe scripts\\export_project_graph.py
```

Что где искать: `venv\\Scripts\\python.exe scripts\\search_project_map.py --graph ИМЯ --depth 2` — окрестность функции,
`--deps ФАЙЛ` — зависимости модуля, `--orphans` — кого никто не вызывает, `--import-cycles` — циклы импортов.
Страница для человека — `/admin/map/graph`.
"""


def _table(rows: list[list[str]], header: list[str]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "/") for c in row) + " |")
    return out


def _degrees(graph: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    out_deg: dict[str, int] = collections.Counter()
    in_deg: dict[str, int] = collections.Counter()
    for edge in graph["edges"]:
        weight = edge.get("n", 1)
        out_deg[edge["from"]] += weight
        in_deg[edge["to"]] += weight
    return dict(out_deg), dict(in_deg)


def _mermaid_subsystems(module_graph: dict[str, Any], *, top: int = 30) -> list[str]:
    paths = G._paths_by_id(module_graph)
    edges: dict[tuple[str, str], int] = collections.Counter()
    for edge in module_graph["edges"]:
        source, target = G.subsystem(paths.get(edge["from"], "")), G.subsystem(paths.get(edge["to"], ""))
        if source != target:
            edges[(source, target)] += edge["n"]
    if not edges:
        return ["_связей между подсистемами нет_"]
    groups = sorted({g for pair in edges for g in pair})
    ids = {name: f"g{n}" for n, name in enumerate(groups)}
    lines = ["```mermaid", "flowchart LR"]
    for name in groups:
        lines.append(f'  {ids[name]}["{name}"]')
    for (source, target), weight in sorted(edges.items(), key=lambda kv: -kv[1])[:top]:
        lines.append(f"  {ids[source]} -->|{weight}| {ids[target]}")
    lines.append("```")
    return lines


def atlas(graph: dict[str, Any], conn: Any = None, *, top: int = 25, routes_limit: int = 200) -> str:
    """Markdown-атлас: подсистемы, данные, интерфейс, окружение, тесты, сироты, циклы, хабы."""
    stats = graph.get("stats") or G.stats(graph)
    index = G.node_index(graph)
    out_deg, in_deg = _degrees(graph)
    modules = G.module_graph(graph)
    lines: list[str] = [BEGIN, ""]
    source = graph.get("source") or {}
    indexed_at = source.get("indexed_at") or ""
    try:
        indexed_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(indexed_at)))
    except (TypeError, ValueError):
        indexed_at = str(indexed_at) or "—"
    lines += [f"_Собрано: {time.strftime('%Y-%m-%d %H:%M', time.localtime(graph.get('built_at') or time.time()))}"
              f" · индекс от {indexed_at} · коммит `{str(source.get('git_head') or '—')[:8]}`"
              f" · узлов {stats['nodes']}, рёбер {stats['edges']}_", ""]

    # 1. Состав графа
    lines += ["## 1. Из чего состоит граф", ""]
    lines += _table([[G.NODE_LABELS.get(kind, kind), count] for kind, count in stats["by_kind"].items()],
                    ["Узлы", "Штук"])
    lines += [""]
    rows = []
    for rel, count in list(stats["by_relation"].items())[:top]:
        label = G.RELATION_LABELS.get(rel, rel[2:] if rel.startswith("m:") else rel)
        rows.append([f"`{rel}`", label, count])
    lines += _table(rows, ["Связь", "Смысл", "Штук"])
    lines += ["", f"Всего связей: **{stats['edges']}**; видов: {len(stats['by_relation'])}.", ""]

    # 2. Подсистемы
    lines += ["## 2. Подсистемы и их связи", ""]
    group_files: dict[str, int] = collections.Counter()
    group_nodes: dict[str, int] = collections.Counter()
    for node in graph["nodes"]:
        if node.get("group"):
            group_nodes[node["group"]] += 1
        if node["kind"] == "file":
            group_files[node["group"]] += 1
    group_in: dict[str, int] = collections.Counter()
    group_out: dict[str, int] = collections.Counter()
    for edge in graph["edges"]:
        source_node, target_node = index.get(edge["from"]), index.get(edge["to"])
        if not source_node or not target_node:
            continue
        source_group, target_group = source_node.get("group"), target_node.get("group")
        if source_group and target_group and source_group != target_group:
            group_out[source_group] += 1
            group_in[target_group] += 1
    rows = [[group, group_files[group], group_nodes[group], group_out[group], group_in[group]]
            for group in sorted(group_nodes, key=lambda g: -group_nodes[g])]
    lines += _table(rows, ["Подсистема", "Файлов", "Узлов", "Связей наружу", "Связей внутрь"])
    lines += ["", "Связи между подсистемами (толщина — число связей):", ""]
    lines += _mermaid_subsystems(modules)
    lines += [""]

    # 3. Данные
    lines += ["## 3. База данных: кто пишет и кто читает", ""]
    table_rows = []
    for node in sorted(graph["nodes"], key=lambda n: -(n.get("writers", 0) + n.get("readers", 0))):
        if node["kind"] != "table":
            continue
        writers = sorted({index[e["from"]]["path"] for e in graph["edges"]
                          if e["to"] == node["id"] and e["rel"] == "writes" and index.get(e["from"], {}).get("path")})
        readers = sorted({index[e["from"]]["path"] for e in graph["edges"]
                          if e["to"] == node["id"] and e["rel"] == "reads" and index.get(e["from"], {}).get("path")})
        table_rows.append([f'`{node["label"]}`', node.get("cols", 0), len(writers), len(readers),
                           ", ".join(f"`{p}`" for p in writers[:4]) or "—"])
    lines += _table(table_rows, ["Таблица", "Столбцов", "Пишущих файлов", "Читающих файлов", "Кто пишет (топ-4)"])
    lines += [""]

    # 4. Интерфейс
    lines += ["## 4. Роуты: права, зависимости, таблицы", ""]
    route_rows = []
    for node in sorted((n for n in graph["nodes"] if n["kind"] == "route"), key=lambda n: n["label"]):
        guards = node.get("guards") or "—"
        deps = node.get("deps") or "—"
        tables = sorted({index[e["to"]]["label"] for e in graph["edges"]
                         if e["from"] == node["id"] and e["rel"] in {"reads", "writes"} and e["to"] in index})
        route_rows.append([f'`{node["label"]}`', f'`{node["path"]}`', node.get("handler") or "—", guards, deps,
                           ", ".join(tables) or "—"])
    lines += _table(route_rows[:routes_limit], ["Роут", "Файл", "Обработчик", "Права", "Зависимости", "Таблицы"])
    if len(route_rows) > routes_limit:
        lines += ["", f"_Показаны первые {routes_limit} из {len(route_rows)} роутов._"]
    lines += [""]

    lines += ["## 5. Кнопки и элементы интерфейса → API", ""]
    ui_rows = []
    for node in sorted((n for n in graph["nodes"] if n["kind"] == "ui"), key=lambda n: n["id"]):
        api = sorted({index[e["to"]]["label"] for e in graph["edges"]
                      if e["from"] == node["id"] and e["rel"] == "calls_api" and e["to"] in index})
        js = sorted({index[e["to"]]["label"] for e in graph["edges"]
                     if e["from"] == node["id"] and e["rel"] == "handled_by" and e["to"] in index})
        if not api and not js:
            continue
        ui_rows.append([f'`#{node["id"].split(":", 1)[1]}`', node["label"][:60], f'`{node.get("path", "")}`',
                        ", ".join(f"`{a}`" for a in api) or "—", ", ".join(f"`{j}`" for j in js) or "—"])
    lines += _table(ui_rows, ["Элемент", "Подпись", "Шаблон", "API", "JS"])
    lines += [""]

    # 5. Окружение
    lines += ["## 6. Переменные окружения и константы", ""]
    env_rows = []
    for node in sorted((n for n in graph["nodes"] if n["kind"] == "env"), key=lambda n: n["label"]):
        readers = sorted({f'{index[e["from"]]["path"]}:{index[e["from"]].get("line") or ""}'
                          for e in graph["edges"] if e["to"] == node["id"] and e["from"] in index})[:3]
        env_rows.append([f'`{node["label"]}`', node.get("sub", ""), (node.get("default") or "")[:40],
                         "да" if node.get("documented") else "нет", ", ".join(readers) or "—"])
    lines += _table(env_rows, ["Имя", "Вид", "Значение", "В .env.example", "Где читается"])
    lines += [""]

    # 6. Промпты
    prompt_rows = []
    for node in sorted((n for n in graph["nodes"] if n["kind"] == "file" and n["path"].startswith("docs/prompts/")),
                       key=lambda n: n["path"]):
        users = sorted({index[e["from"]]["label"] if index.get(e["from"], {}).get("kind") == "manifest"
                        else index[e["from"]]["path"] for e in graph["edges"]
                        if e["to"] == node["id"] and e["from"] in index})
        prompt_rows.append([f'`{node["path"]}`', ", ".join(f"`{u}`" for u in users) or "—"])
    if prompt_rows:
        lines += ["## 7. Промпты и кто их использует", ""]
        lines += _table(prompt_rows, ["Промпт", "Использует"])
        lines += [""]

    # 7. Тесты
    lines += ["## 8. Покрытие тестами", ""]
    tested: dict[str, set[str]] = collections.defaultdict(set)
    for edge in graph["edges"]:
        if edge["rel"] != "covers":
            continue
        test_node, target = index.get(edge["from"]), index.get(edge["to"])
        if target and target.get("path"):
            tested[target["path"]].add(test_node.get("path", "") if test_node else "")
    files = [n["path"] for n in graph["nodes"] if n["kind"] == "file" and n["sub"] == "py"]
    without = [p for p in files if p not in tested and "/tests/" not in p and not p.startswith("scripts/")]
    rows = [[G.subsystem(path), path, len(tested.get(path, ()))] for path in sorted(tested, key=lambda p: -len(tested[p]))[:top]]
    lines += _table(rows, ["Подсистема", "Файл", "Тестовых файлов"]) if rows else ["_нет данных_"]
    lines += ["", f"Файлов Python без тестов: **{len(without)}** из {len(files)}. Первые {min(top, len(without))}:", ""]
    lines += ["- " + "\n- ".join(f"`{p}`" for p in without[:top]) if without else "_все под тестами_"]
    lines += [""]

    # 8. Хабы, сироты, циклы
    lines += ["## 9. Пересечения: самые связанные узлы", ""]
    hubs = sorted(graph["nodes"], key=lambda n: -(out_deg.get(n["id"], 0) + in_deg.get(n["id"], 0)))[:top]
    lines += _table([[f'`{n["label"]}`', G.NODE_LABELS.get(n["kind"], n["kind"]),
                      f'`{n.get("path", "")}:{n.get("line") or ""}`', out_deg.get(n["id"], 0), in_deg.get(n["id"], 0)]
                     for n in hubs], ["Узел", "Вид", "Адрес", "Исходящих", "Входящих"])
    lines += [""]

    if conn is not None:
        orphans = G.orphans(conn, limit=top * 2)
        lines += ["## 10. Никто не вызывает (проверить: мёртвый код или точка входа)", "",
                  "_Строгий список: у функции нет ни вызовов, ни тестов, ни роута, ни декоратора и её имя не упоминается "
                  "в коде других файлов (упоминания в документации находку не отменяют). Динамическая регистрация "
                  "(`getattr`, списки) статикой не видна — проверяйте глазами._", ""]
        lines += _table([[f'`{o["name"]}`', f'`{o["path"]}:{o["line"]}`', o["kind"]] for o in orphans],
                        ["Функция", "Адрес", "Вид"]) if orphans else ["_таких нет_"]
        lines += ["", f"Всего: {len(G.orphans(conn, limit=10_000))}.", ""]
        cycles = G.import_cycles(conn, limit=top)
        lines += ["## 11. Циклы импортов", ""]
        if cycles:
            for component in cycles:
                lines.append("- " + " → ".join(f"`{p}`" for p in component))
        else:
            lines.append("_циклов нет_")
        lines += [""]

    lines += ["## 12. Как обновлять", "",
              "1. После правок кода — `venv\\Scripts\\python.exe scripts\\index_project_map.py` (обновляет индекс).",
              "2. Затем — `venv\\Scripts\\python.exe scripts\\export_project_graph.py` (пересобирает граф и этот атлас).",
              "3. Расхождения ловит тест `app/app/tests/test_project_graph.py` (у каждого файла и роута есть узел, висячих рёбер нет).",
              "", END, ""]
    return "\n".join(lines)


def write_atlas(text: str, path: Any = None) -> Any:
    """Записать атлас между маркерами, сохранив рукописную часть файла."""
    target = path or ATLAS_PATH
    if target.is_file():
        current = target.read_text(encoding="utf-8")
        if BEGIN in current and END in current:
            head = current.split(BEGIN)[0]
            tail = current.split(END)[1]
            body = head + text + tail.lstrip("\n")
        else:
            body = current.rstrip() + "\n\n" + text
    else:
        body = ATLAS_INTRO + "\n" + text
    target.write_text(body, encoding="utf-8", newline="")
    return target


def summary_text(graph: dict[str, Any]) -> str:
    """Короткая сводка графа для терминала и MCP."""
    stats = graph.get("stats") or G.stats(graph)
    source = graph.get("source") or {}
    lines = [f"граф проекта: узлов {stats['nodes']}, рёбер {stats['edges']}",
             f"индекс от {source.get('indexed_at', '—')}, коммит {str(source.get('git_head') or '—')[:8]}"]
    lines.append("узлы: " + ", ".join(f"{G.NODE_LABELS.get(k, k)} {v}" for k, v in stats["by_kind"].items()))
    lines.append("связи: " + ", ".join(f"{k} {v}" for k, v in list(stats["by_relation"].items())[:12]))
    return "\n".join(lines)


def neighbors_text(result: dict[str, Any], *, limit: int = 40) -> str:
    """Окрестность узла человеческим текстом: входящие, исходящие, сводка."""
    center = result.get("center")
    if not center:
        return "узел не найден"
    index = {n["id"]: n for n in result["nodes"]}
    incoming: dict[str, list[str]] = collections.defaultdict(list)
    outgoing: dict[str, list[str]] = collections.defaultdict(list)
    for edge in result["edges"]:
        source, target = index.get(edge["from"], {}), index.get(edge["to"], {})
        label = lambda n: f'{n.get("label", "?")} ({n.get("path", "")}:{n.get("line") or ""})'  # noqa: E731
        outgoing[edge["from"]].append(f'{edge["rel"]} → {label(target)}' + (f' ×{edge["n"]}' if edge.get("n", 1) > 1 else ""))
        incoming[edge["to"]].append(f'{edge["rel"]} ← {label(source)}' + (f' ×{edge["n"]}' if edge.get("n", 1) > 1 else ""))
    lines = [f'{center["label"]}  [{G.NODE_LABELS.get(center["kind"], center["kind"])}]  '
             f'{center.get("path", "")}:{center.get("line") or ""}']
    if center.get("sub"):
        lines.append(f'вид: {center["sub"]}' + (f' · {center["sig"]}' if center.get("sig") else ""))
    lines.append(f'исходящих связей: {len(outgoing.get(center["id"], []))}, входящих: {len(incoming.get(center["id"], []))}')
    for title, items in (("вызывает / влияет", outgoing.get(center["id"], [])),
                         ("вызывают / влияют", incoming.get(center["id"], []))):
        if not items:
            continue
        lines.append(f"{title}:")
        for item in sorted(set(items))[:limit]:
            lines.append("  " + item)
    if result.get("truncated"):
        lines.append("⚠ окрестность обрезана по размеру — увеличьте --limit или уменьшите --depth")
    for note in result.get("notes") or []:
        lines.append("· " + note)
    return "\n".join(lines)


def module_deps_text(graph: dict[str, Any], query: str, *, limit: int = 30) -> str:
    """Зависимости модуля: от кого зависит, кто зависит, какими таблицами и роутами владеет."""
    node = G.find_node(graph, query)
    if node is None:
        return "модуль не найден"
    path = node.get("path") or node["label"]
    index = G.node_index(graph)
    imports_out: collections.Counter = collections.Counter()
    imports_in: collections.Counter = collections.Counter()
    calls_out: collections.Counter = collections.Counter()
    calls_in: collections.Counter = collections.Counter()
    tables: dict[str, set[str]] = collections.defaultdict(set)
    for edge in graph["edges"]:
        source, target = index.get(edge["from"]), index.get(edge["to"])
        if not source or not target:
            continue
        if edge["rel"].startswith("import"):
            if source.get("path") == path:
                imports_out[target.get("path") or target["label"]] += edge.get("n", 1)
            if target.get("path") == path:
                imports_in[source.get("path") or source["label"]] += edge.get("n", 1)
        elif edge["rel"].startswith("calls"):
            if source.get("path") == path:
                calls_out[f'{target.get("label")} ({target.get("path")}:{target.get("line") or ""})'] += edge.get("n", 1)
            if target.get("path") == path:
                calls_in[f'{source.get("label")} ({source.get("path")}:{source.get("line") or ""})'] += edge.get("n", 1)
        elif edge["rel"] in {"reads", "writes"}:
            if source.get("path") == path and target["kind"] == "table":
                tables[target["label"]].add(edge["rel"])
    routes = [n for n in graph["nodes"] if n["kind"] == "route" and n.get("path") == path]
    lines = [f"{path}  [{G.subsystem(path)}]",
             f"импортирует: {len(imports_out)} файлов · его импортируют: {len(imports_in)}",
             f"вызовов наружу: {sum(calls_out.values())} · вызовов внутрь: {sum(calls_in.values())}",
             f"роутов в файле: {len(routes)}"]
    for title, data in (("зависит от (импорт)", imports_out), ("зависят от него", imports_in),
                        ("вызывает", calls_out), ("вызывают его", calls_in)):
        if data:
            lines.append(f"{title}:")
            for name, count in data.most_common(limit):
                lines.append(f"  {name}" + (f" ×{count}" if count > 1 else ""))
    if tables:
        lines.append("таблицы: " + ", ".join(f"{t} ({'/'.join(sorted(r))})" for t, r in sorted(tables.items())))
    if routes:
        lines.append("роуты: " + ", ".join(f'{r["label"]} → {r.get("handler")}' for r in routes))
    return "\n".join(lines)


def orphans_text(rows: list[dict], *, total: int | None = None) -> str:
    lines = [f'функций без вызовов: {total if total is not None else len(rows)}']
    for row in rows:
        lines.append(f'  {row["name"]}  {row["path"]}:{row["line"]}  [{row["kind"]}]')
    return "\n".join(lines)


def cycles_text(components: list[list[str]]) -> str:
    if not components:
        return "циклов импортов нет"
    lines = [f"циклов: {len(components)}"]
    for component in components:
        lines.append("  " + " → ".join(component))
    return "\n".join(lines)
