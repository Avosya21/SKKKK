"""Отчёт по метрикам графа: разделы Markdown для атласа и документов (группы, хабы, правки, скрытая связность)."""

from __future__ import annotations

from typing import Any


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return lines


def render_sections(summary: dict[str, Any], first_number: int) -> tuple[list[str], int]:
    """Разделы атласа по метрикам; возвращает строки и номер следующего свободного раздела."""
    number = first_number
    lines: list[str] = []
    options = summary.get("options", {})

    lines += [f"## {number}. Группы по связности (Louvain)", "",
              f"Файлы без тестов, связанные вызовами, импортами и наследованием, разбиты на группы алгоритмом Louvain "
              f"(разрешение {options.get('resolution')}, запусков {options.get('seeds')}, берётся лучший по модулярности). "
              f"Модулярность **{summary.get('modularity')}** (выше 0,3 — группы выражены); согласие групп с заданными подсистемами "
              f"(NMI, 1 — совпадают) **{summary.get('agreement_with_groups')}**; в группах {summary.get('files_in_communities')} из "
              f"{summary.get('files_in_graph')} файлов.", ""]
    rows = []
    for c in summary.get("communities", [])[:25]:
        rows.append([c["id"], c["size"], f"{c['group']} ({round(c['group_share'] * 100)}%)", ", ".join(c.get("keywords", [])) or "—",
                     ", ".join(f"`{p}`" for p in c.get("top_files", []))])
    lines += _table(["Группа", "Файлов", "Подсистема (доля)", "Ключевые слова", "Главные файлы"], rows) if rows else ["_групп нет_"]
    lines += [""]
    number += 1

    lines += [f"## {number}. Узлы-перекрёстки и рейтинги связности", "",
              f"Fan-in — сколько других файлов зависят от файла, fan-out — от скольких файлов зависит он сам. «Хаб» — оба не меньше "
              f"{options.get('hub_min')}: такие файлы дороже всего менять.", ""]
    hubs = summary.get("hubs", [])
    lines += _table(["Хаб", "Fan-in", "Fan-out"], [[f"`{h['path']}`", h["fan_in"], h["fan_out"]] for h in hubs]) if hubs else ["_хабов нет_"]
    lines += ["", "**Больше всего зависят от файла (fan-in)**", ""]
    lines += _table(["Файл", "Fan-in"], [[f"`{r['path']}`", r["value"]] for r in summary.get("top_fan_in", [])])
    lines += ["", "**Больше всего зависит файл (fan-out)**", ""]
    lines += _table(["Файл", "Fan-out"], [[f"`{r['path']}`", r["value"]] for r in summary.get("top_fan_out", [])])
    lines += [""]
    number += 1

    misplaced = summary.get("misplaced", [])
    lines += [f"## {number}. Файлы в «чужой» группе", "",
              "Файл связан кодом в основном с другой подсистемой, чем та, где он лежит: кандидат на перенос или признак скрытой зависимости. "
              "Показаны группы, где одна подсистема составляет большинство.", ""]
    lines += _table(["Файл", "Лежит в", "Живёт в группе", "Группа №"],
                    [[f"`{m['path']}`", m["group"], m["community_group"], m["community"]] for m in misplaced[:30]]) if misplaced else ["_таких нет_"]
    lines += [f"", f"Всего: {len(misplaced)}.", ""]
    number += 1

    lines += [f"## {number}. Частота правок и скрытая связность", "",
              "Частота правок — в скольких коммитах менялся файл. Скрытая связность — файлы, которые часто меняют вместе, хотя прямой связи "
              f"в коде между ними нет (пар с совместными правками: {summary.get('cochange_pairs', 0)}).", ""]
    lines += _table(["Файл", "Коммитов"], [[f"`{r['path']}`", r["commits"]] for r in summary.get("top_churn", [])])
    lines += ["", "**Меняются вместе без связи в коде**", ""]
    hidden = summary.get("cochange_hidden", [])
    lines += _table(["Файл", "Файл", "Коммитов вместе"], [[f"`{h['a']}`", f"`{h['b']}`", h["commits"]] for h in hidden]) if hidden else ["_таких пар нет_"]
    lines += [""]
    number += 1
    return lines, number
