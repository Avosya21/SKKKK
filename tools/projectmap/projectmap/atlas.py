"""Атлас графа проекта: короткий документ Markdown о том, из чего состоит карта, как она разбита на подсистемы и что показывают метрики.

Это отчёт самого инструмента (`projectmap graph`); он не зависит от проекта. Проектные атласы с рукописной шапкой строят потребители поверх
тех же данных (граф и `graph["metrics"]`).
"""

from __future__ import annotations

import time
from typing import Any

from projectmap.metrics_report import render_sections
from projectmap.schema import NODE_KINDS, RELATIONS


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]


def render_atlas(graph: dict[str, Any]) -> str:
    stats = graph.get("stats") or {}
    built = time.strftime("%Y-%m-%d %H:%M", time.localtime(graph.get("built_at") or time.time()))
    lines = ["# Атлас графа проекта", "",
             f"_Собрано {built} инструментом projectmap · узлов {stats.get('nodes', 0)}, рёбер {stats.get('edges', 0)} · файлов {graph.get('source', {}).get('files', 0)}_", ""]
    lines += ["## 1. Из чего состоит граф", ""]
    lines += _table(["Узлы", "Штук"], [[NODE_KINDS.get(kind, ("", kind))[1], count] for kind, count in (stats.get("by_kind") or {}).items()])
    lines += [""]
    lines += _table(["Связь", "Смысл", "Штук"], [[f"`{rel}`", RELATIONS.get(rel, ("", rel[2:] if rel.startswith("m:") else rel))[1], count]
                                             for rel, count in list((stats.get("by_relation") or {}).items())[:30]])
    lines += ["", "## 2. Подсистемы", ""]
    groups = stats.get("by_group") or {}
    lines += _table(["Подсистема", "Узлов"], [[name, count] for name, count in list(groups.items())[:40]]) if groups else ["_подсистем нет_"]
    lines += [""]
    next_number = 3
    if graph.get("metrics"):
        section, next_number = render_sections(graph["metrics"], next_number)
        lines += section
    if graph.get("notes"):
        lines += [f"## {next_number}. Примечания сборки", ""] + [f"- {note}" for note in graph["notes"]] + [""]
    return "\n".join(lines).rstrip() + "\n"
