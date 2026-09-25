"""HTML → текст с таблицами: блоки разметки дают переносы строк, ``<table>`` — markdown-таблицы на своих местах.

Раньше ``text_content()`` склеивал ячейки без разделителей («1параметр2руб/кг100»), и таблица терялась. Таблица
разбирается в сетку (``colspan``/``rowspan`` разворачиваются значением), линейка выгрузки Google Sheets / Excel
(строка A, B, C… и столбец номеров строк) убирается, дальше — те же правила, что у листа Excel
(``xlsx_extract.grid_markdown``: таблицы по блокам между пустыми строками, подписи, двухуровневая шапка).
Вёрсточные таблицы (с вложенными таблицами) остаются текстом.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.app.services.table_md import clean_cell

BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer", "main", "nav",
    "aside", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "hr", "figure", "figcaption",
}
MAX_SPAN = 60
MAX_TABLE_ROWS = 5000
_COLUMN_LETTERS = re.compile(r"^[A-Z]{1,3}$")


def _read_html(path: Path) -> str:
    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _span(cell, name: str) -> int:
    try:
        return max(1, min(MAX_SPAN, int(cell.get(name) or 1)))
    except ValueError:
        return 1


def table_grid(table) -> tuple[list[list[str]], dict[int, set[int]]]:
    """``<table>`` → сетка строк; объединённые ячейки заполняются значением, строки с ``colspan`` отмечаются."""
    grid: list[list[str]] = []
    merged_rows: dict[int, set[int]] = {}
    carry: dict[tuple[int, int], str] = {}  # (строка, столбец) → значение из rowspan выше
    rows = table.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr")[:MAX_TABLE_ROWS]
    for r, tr in enumerate(rows):
        row: list[str] = []
        col = 0
        for cell in tr.xpath("./td|./th"):
            while (r, col) in carry:
                row.append(carry.pop((r, col)))
                col += 1
            text = clean_cell(cell.text_content())
            colspan, rowspan = _span(cell, "colspan"), _span(cell, "rowspan")
            if colspan > 1:
                merged_rows.setdefault(r, set()).update(range(col, col + colspan))
            for _ in range(colspan):
                row.append(text)
                for down in range(1, rowspan):
                    carry[(r + down, col)] = text
                col += 1
        while (r, col) in carry:
            row.append(carry.pop((r, col)))
            col += 1
        grid.append(row)
    return grid, merged_rows


def drop_sheet_ruler(grid: list[list[str]], merged_rows: dict[int, set[int]]):
    """Выгрузка таблицы как веб-страницы: первая строка A, B, C…, первый столбец 1, 2, 3… — это линейка, не данные."""
    if len(grid) < 2:
        return grid, merged_rows
    head = [c for c in grid[0][1:] if c.strip()]
    if len(head) < 2 or not all(_COLUMN_LETTERS.match(c) for c in head) or grid[0][0].strip():
        return grid, merged_rows
    numbers = [r[0] for r in grid[1:] if r and r[0].strip()]
    if not numbers or not all(n.isdigit() for n in numbers):
        return grid, merged_rows
    body = [r[1:] for r in grid[1:]]
    shifted = {k - 1: {c - 1 for c in cols if c > 0} for k, cols in merged_rows.items() if k > 0}
    return body, shifted


def extract_html(path: Path) -> str:
    from lxml import etree
    from lxml import html as lxml_html

    from app.app.services.xlsx_extract import grid_markdown

    doc = lxml_html.fromstring(_read_html(path))
    for el in doc.xpath("//script|//style|//noscript|//head"):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)

    table_no = 0
    for table in doc.xpath("//table[not(.//table)]"):
        grid, merged_rows = table_grid(table)
        grid, merged_rows = drop_sheet_ruler(grid, merged_rows)
        parts, new_no = grid_markdown(grid, merged_rows, table_no)
        if new_no == table_no:  # нет настоящей таблицы (одна колонка, одна строка) — оставляем текстом
            continue
        table_no = new_no
        block = etree.Element("div")
        block.text = "\n\n" + "\n\n".join(parts) + "\n\n"
        block.tail = table.tail
        table.getparent().replace(table, block)

    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.lower()
        if tag in BLOCK_TAGS:
            el.tail = "\n" + (el.tail or "")
        elif tag in ("td", "th"):
            el.tail = " " + (el.tail or "")

    lines = [ln.strip() if ln.lstrip().startswith("|") else " ".join(ln.split()) for ln in doc.text_content().splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
