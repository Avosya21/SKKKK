"""Таблицы документов в markdown: один формат для DOCX, Excel/CSV, PDF и разбор его обратно (нормализация, нарезка).

Таблица в тексте — строка подписи ``Таблица N: …`` (необязательна) и GFM-таблица ``| шапка |`` + ``| --- |`` + строки.
Маркеров сверх GFM нет: тот же текст читают отчёты, выжимка и чат, а таблицу находят по разделителю шапки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CAPTION_PREFIX = "Таблица"
_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_CAPTION_RE = re.compile(rf"^{CAPTION_PREFIX}\s+\d+[.:]?\s*")


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]
    caption: str = ""


@dataclass
class Block:
    kind: str  # "text" | "table"
    text: str
    table: Table | None = None
    headings: list[str] = field(default_factory=list)  # путь разделов (# … ###) над блоком


def clean_cell(value: object, *, soft_wrap: bool = False) -> str:
    """Текст ячейки в одну строку: переносы → «; » (абзацы ячейки) или пробел (``soft_wrap`` — перенос по ширине
    в PDF), пробелы схлопнуты, ``|`` экранирован."""
    if value is None:
        return ""
    lines = [" ".join(ln.split()) for ln in str(value).replace("\r", "\n").split("\n")]
    text = (" " if soft_wrap else "; ").join(ln for ln in lines if ln)
    return text.replace("|", "\\|")


def _is_blank_row(row: list[str]) -> bool:
    return not any(c.strip() for c in row)


def make_table(grid: list[list[str]], caption: str = "") -> Table | None:
    """Сетка строк → Table: первая непустая строка — шапка, пустые строки и столбцы убраны, ширина выровнена."""
    rows = [list(r) for r in grid if not _is_blank_row(list(r))]
    if not rows:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [j for j in range(width) if any(r[j].strip() for r in rows)]
    rows = [[r[j] for j in keep] for r in rows]
    header = [h or f"Столбец {j + 1}" for j, h in enumerate(rows[0])]
    return Table(header=header, rows=rows[1:], caption=caption)


def _md_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def render_md(table: Table, *, number: int | None = None) -> str:
    lines: list[str] = []
    if number is not None or table.caption:
        head = f"{CAPTION_PREFIX} {number}" if number is not None else CAPTION_PREFIX
        lines.append(f"{head}: {table.caption}" if table.caption else head)
    lines.append(_md_row(table.header))
    lines.append(_md_row(["---"] * len(table.header)))
    lines += [_md_row(r) for r in table.rows]
    return "\n".join(lines)


def render_header(table: Table) -> str:
    return _md_row(table.header) + "\n" + _md_row(["---"] * len(table.header))


def render_record(table: Table, row: list[str]) -> str:
    """Строка таблицы самодостаточной записью: ``Столбец: значение; …`` (пустые ячейки пропускаются)."""
    pairs = [f"{h}: {v}" for h, v in zip(table.header, row) if v.strip()]
    return "; ".join(pairs)


def split_md_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    cells = re.split(r"(?<!\\)\|", body)
    return [c.strip() for c in cells]


def is_separator_line(line: str) -> bool:
    s = line.strip()
    if not s.startswith("|"):
        return False
    cells = split_md_row(s)
    return bool(cells) and all(_SEPARATOR_CELL.match(c.replace(" ", "")) for c in cells)


def _is_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def split_blocks(text: str) -> list[Block]:
    """Текст → блоки по порядку: абзацы прозы и GFM-таблицы (с подписью ``Таблица N`` над ними, если есть).

    У каждого блока — путь заголовков markdown (``#``), под которыми он стоит.
    """
    lines = text.splitlines()
    blocks: list[Block] = []
    headings: list[tuple[int, str]] = []
    prose: list[str] = []

    def flush_prose() -> None:
        body = "\n".join(prose).strip()
        if body:
            blocks.append(Block("text", body, headings=[h for _lvl, h in headings]))
        prose.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if m:
            flush_prose()
            level = len(m.group(1))
            headings = [(lvl, h) for lvl, h in headings if lvl < level]
            headings.append((level, m.group(2).strip()))
            prose.append(line)
            i += 1
            continue
        if _is_table_row(line) and i + 1 < len(lines) and is_separator_line(lines[i + 1]):
            caption = ""
            caption_line = ""
            if prose and _CAPTION_RE.match(prose[-1].strip()):
                caption_line = prose.pop().strip()
                caption = _CAPTION_RE.sub("", caption_line)
            flush_prose()
            header = split_md_row(line)
            j = i + 2
            rows: list[list[str]] = []
            while j < len(lines) and _is_table_row(lines[j]):
                cells = split_md_row(lines[j])
                cells = (cells + [""] * len(header))[: max(len(header), len(cells))]
                rows.append(cells)
                j += 1
            table = Table(header=header, rows=rows, caption=caption)
            raw = "\n".join(lines[i:j])
            if caption_line:
                raw = f"{caption_line}\n{raw}"
            blocks.append(Block("table", raw, table=table, headings=[h for _lvl, h in headings]))
            i = j
            continue
        prose.append(line)
        i += 1
    flush_prose()
    return blocks


def table_numbers(text: str) -> list[str]:
    """Все числа из строк таблиц (для проверки «цифры не потеряны»); пробелы в разрядах схлопнуты."""
    out: list[str] = []
    for block in split_blocks(text):
        if block.kind != "table" or block.table is None:
            continue
        for row in block.table.rows:
            for cell in row:
                out += [n.replace(" ", "").replace(" ", "") for n in re.findall(r"\d[\d  ]*(?:[.,]\d+)?", cell)]
    return [n for n in out if n]
