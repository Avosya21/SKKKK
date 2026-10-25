"""Excel/CSV → markdown: ``## Лист: имя`` и GFM-таблицы по блокам листа.

- .xlsx/.xlsm — openpyxl, значения формул (кэш Excel); формула без кэша показывается текстом ``=…``.
- .xls/.ods — сначала LibreOffice → .xlsx.
- .csv/.tsv — разделитель через csv.Sniffer, кодировка как у текстовых файлов.
Лист режется на таблицы по пустым строкам; объединённые ячейки разворачиваются значением; строка с одной
ячейкой над таблицей — подпись таблицы; двухуровневая шапка склеивается «верх / низ»; шапка, отбитая от данных
пустыми строками, приклеивается к ним. Примечания ячеек — в той же ячейке «[примечание: …]». Скрытые листы пропускаются.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import tempfile
from datetime import date, datetime, time
from pathlib import Path

from app.app.logging_setup import log_event
from app.app.services.table_md import clean_cell, make_table, render_md

logger = logging.getLogger("primaryanalitics.xlsx")

SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xls", ".ods"}
CSV_SUFFIXES = {".csv", ".tsv"}
LEGACY_SHEET_SUFFIXES = {".xls", ".ods"}
MAX_ROWS_PER_SHEET = 5000
MAX_COLS = 60
FORMULA_WORKBOOK_MAX_BYTES = 8 * 1024 * 1024  # второй проход (тексты формул) — только для небольших книг


def _fmt_number(value: float | int, number_format: str) -> str:
    fmt = (number_format or "").lower()
    if "%" in fmt:
        pct = float(value) * 100
        return f"{pct:.2f}".rstrip("0").rstrip(".") + "%"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    grouped = "#,##0" in fmt or "# ##0" in fmt
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ") if grouped else str(value)
    decimals = fmt.split(".", 1)[1].count("0") if "." in fmt else None
    if decimals:
        text = f"{value:,.{decimals}f}" if grouped else f"{value:.{decimals}f}"
    else:
        text = f"{value:,.10f}" if grouped else f"{value:.10f}"
        text = text.rstrip("0").rstrip(".")
    return text.replace(",", " ").replace(".", ",") if grouped else text


def format_value(value: object, number_format: str = "") -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, datetime):
        if value.time() == time(0, 0):
            return value.strftime("%d.%m.%Y")
        return value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, (int, float)):
        return _fmt_number(value, number_format)
    return clean_cell(value)


def _split_grid(grid: list[list[str]]) -> list[list[list[str]]]:
    """Лист → блоки подряд идущих непустых строк."""
    blocks: list[list[list[str]]] = []
    cur: list[list[str]] = []
    for row in grid:
        if any(c.strip() for c in row):
            cur.append(row)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def _looks_textual(row: list[str]) -> bool:
    filled = [c for c in row if c.strip()]
    if not filled:
        return False
    numeric = sum(1 for c in filled if c.replace(" ", "").replace(",", "").replace(".", "").replace("%", "").lstrip("-").isdigit())
    return numeric == 0


def _is_caption_row(row: list[str], width: int) -> bool:
    """Одна надпись над таблицей: одно заполненное значение (или одно, растянутое объединением на всю строку)."""
    filled = [c for c in row if c.strip()]
    return width > 2 and bool(filled) and len(set(filled)) == 1 and (len(filled) == 1 or len(filled) >= width - 1)


def _is_sparse_above(row: list[str], below: list[str], width: int) -> bool:
    filled = sum(1 for c in row if c.strip())
    under = sum(1 for c in below if c.strip())
    return width > 3 and filled * 2 <= width and under >= 2 * max(filled, 1)


def _block_to_table(block: list[list[str]], start: int, merged_rows: dict[int, set[int]]):
    caption = ""
    rows = [list(r) for r in block]
    width = max(len(r) for r in rows)
    # Надписи над таблицей: строка с одним значением или заметно реже заполненная, чем строка под ней
    # («ПЛАН | 2 000 | 200 000» над шапкой из 50 столбцов) — подпись, а не шапка.
    while len(rows) > 2 and (_is_caption_row(rows[0], width) or _is_sparse_above(rows[0], rows[1], width)):
        text = "; ".join(dict.fromkeys(c for c in rows[0] if c.strip()))
        caption = f"{caption}; {text}" if caption else text
        rows = rows[1:]
        start += 1
    if len(rows) == 2 and _is_caption_row(rows[0], width):
        caption = f"{caption}; {next(c for c in rows[0] if c.strip())}" if caption else next(c for c in rows[0] if c.strip())
        rows = rows[1:]
    # Двухуровневая шапка: в верхней строке есть объединение по горизонтали, под ней — текстовая строка.
    if merged_rows.get(start) and len(rows) > 2 and _looks_textual(rows[1]):
        top, low = rows[0], rows[1]
        combined = []
        last_top = ""
        for j in range(max(len(top), len(low))):
            a = top[j] if j < len(top) else ""
            b = low[j] if j < len(low) else ""
            if not a and b and j > 0:
                a = last_top  # пустая ячейка верхней строки справа от значения — продолжение объединения
            last_top = a or last_top
            combined.append(f"{a} / {b}" if a and b and a != b else (a or b))
        rows = [combined] + rows[2:]
    return make_table(rows, caption=caption)


def _detached_header(block: list[list[str]]) -> bool:
    """Одна строка из 3+ значений, в основном текстовых (у Ганта в шапке ещё и даты шкалы), над пустыми строками —
    шапка таблицы, отбитая от данных."""
    if len(block) != 1:
        return False
    filled = [c for c in block[0] if c.strip()]
    textual = sum(1 for c in filled if _looks_textual([c]))
    return len(filled) >= 3 and textual * 2 >= len(filled)


def _glue_header(header: list[str], block: list[list[str]]) -> list[list[str]]:
    """Шапка + строки данных; столбцы, пустые во всех строках данных (шкала дат Ганта с заливкой), убираются."""
    rows = [header, *block]
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [j for j in range(width) if any(r[j].strip() for r in block if j < len(r))]
    return [[r[j] for j in keep] for r in rows]


def grid_markdown(
    grid: list[list[str]], merged_rows: dict[int, set[int]], table_no: int, *, default_caption: str = ""
) -> tuple[list[str], int]:
    """Сетка листа (Excel, таблица HTML) → markdown-блоки: таблицы по блокам между пустыми строками, надписи
    над таблицей — её подпись. ``merged_rows`` — строки с горизонтальными объединениями (для двухуровневой шапки).
    Шапка, отделённая от данных пустыми строками, приклеивается к следующему блоку."""
    parts: list[str] = []
    index_of = {id(row): i for i, row in enumerate(grid)}
    pending: list[str] = []  # надписи над таблицей, отделённые пустой строкой, — подпись следующей таблицы
    blocks = _split_grid(grid)
    k = 0
    while k < len(blocks):
        block = blocks[k]
        k += 1
        width = max(len(r) for r in block)
        if len(block) == 1 and (_is_caption_row(block[0], width) or sum(1 for c in block[0] if c.strip()) <= 2):
            pending.append(" ".join(dict.fromkeys(c for c in block[0] if c.strip())))
            continue
        start = index_of[id(block[0])]
        if _detached_header(block) and k < len(blocks) and not _detached_header(blocks[k]):
            block = _glue_header(block[0], blocks[k])
            k += 1
        table = _block_to_table(block, start, merged_rows)
        if table is None:
            continue
        table_no += 1
        table.caption = "; ".join([*pending, table.caption] if table.caption else pending) or default_caption
        pending = []
        parts.append(render_md(table, number=table_no))
    parts += pending  # надписи в конце листа без таблицы под ними
    return [p for p in parts if p.strip()], table_no


def comment_text(comment) -> str:
    """Текст примечания ячейки. У ветки комментариев нового Excel служебная преамбула «[Threaded comment] …»
    отрезается, ответы («Reply:») идут через « / »; автор не нужен (у веток это идентификатор, не имя)."""
    text = (getattr(comment, "text", "") or "").strip()
    if text.startswith("[Threaded comment]"):
        text = text.split("\nComment:", 1)[1] if "\nComment:" in text else ""
        text = re.sub(r"\s*\n\s*Reply:\s*", " / ", text)
    return clean_cell(text)


def _sheet_markdown(title: str, grid: list[list[str]], merged_rows: dict[int, set[int]], table_no: int) -> tuple[str, int]:
    parts, table_no = grid_markdown(grid, merged_rows, table_no, default_caption=f"лист «{title}»")
    return "\n\n".join([f"## Лист: {title}", *parts]), table_no


def _extract_xlsx(path: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), data_only=True)
    wb_formulas = None
    if path.stat().st_size <= FORMULA_WORKBOOK_MAX_BYTES:
        try:
            wb_formulas = load_workbook(str(path), data_only=False)
        except Exception:
            wb_formulas = None

    sheets: list[str] = []
    table_no = 0
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            log_event(logger, "xlsx.sheet_hidden_skipped", file=path.name, sheet=ws.title)
            continue
        # max_row/max_column учитывают пустые отформатированные ячейки — границы берём по заполненным.
        filled = [key for key, cell in ws._cells.items() if cell.value is not None or cell.comment is not None]
        real_rows = max((r for r, _c in filled), default=0)
        real_cols = max((c for _r, c in filled), default=0)
        max_row = min(real_rows, MAX_ROWS_PER_SHEET)
        max_col = min(real_cols, MAX_COLS)
        if not max_row or not max_col:
            continue
        wsf = wb_formulas[ws.title] if wb_formulas is not None else None
        grid: list[list[str]] = []
        for r_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col), start=1):
            out: list[str] = []
            for c_idx, cell in enumerate(row, start=1):
                value = cell.value
                if value is None and wsf is not None:
                    raw = wsf.cell(row=r_idx, column=c_idx).value
                    if isinstance(raw, str) and raw.startswith("="):
                        value = raw
                text = format_value(value, cell.number_format or "")
                note = comment_text(cell.comment) if cell.comment is not None else ""
                if note:
                    text = f"{text} [примечание: {note}]" if text else f"[примечание: {note}]"
                out.append(text)
            grid.append(out)
        # Объединённые ячейки: значение левой верхней во все ячейки диапазона.
        merged_header_rows: dict[int, set[int]] = {}
        for rng in ws.merged_cells.ranges:
            r0, c0, r1, c1 = rng.min_row - 1, rng.min_col - 1, rng.max_row - 1, rng.max_col - 1
            if r0 >= len(grid) or c0 >= max_col:
                continue
            val = grid[r0][c0]
            for r in range(r0, min(r1, len(grid) - 1) + 1):
                for c in range(c0, min(c1, max_col - 1) + 1):
                    grid[r][c] = val
            if c1 > c0 and (c1 - c0 + 1) < max_col:  # объединение не на всю ширину — кандидат в шапку
                merged_header_rows.setdefault(r0, set()).update(range(c0, c1 + 1))
        if real_rows > MAX_ROWS_PER_SHEET or real_cols > MAX_COLS:
            log_event(logger, "xlsx.truncated", file=path.name, sheet=ws.title, rows=real_rows, cols=real_cols)
        md, table_no = _sheet_markdown(ws.title, grid, merged_header_rows, table_no)
        if real_rows > MAX_ROWS_PER_SHEET:
            md += f"\n\n(показаны строки 1–{MAX_ROWS_PER_SHEET} из {real_rows})"
        if real_cols > MAX_COLS:
            md += f"\n\n(показаны столбцы 1–{MAX_COLS} из {real_cols})"
        if md.strip() != f"## Лист: {ws.title}":
            sheets.append(md)
    return "\n\n".join(sheets).strip()


def _extract_csv(path: Path) -> str:
    from app.app.services.pipeline_documents import _decode_text_bytes

    data = path.read_bytes()
    if not data.strip():
        return ""
    text = _decode_text_bytes(data).replace("\r\n", "\n").replace("\r", "\n")
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    total = len(rows)
    rows = rows[: MAX_ROWS_PER_SHEET + 1]
    grid = [[clean_cell(c) for c in r[:MAX_COLS]] for r in rows]
    table = make_table(grid, caption=f"файл «{path.name}»")
    if table is None:
        return ""
    md = render_md(table, number=1)
    if total > MAX_ROWS_PER_SHEET + 1:
        md += f"\n\n(показаны строки 1–{MAX_ROWS_PER_SHEET} из {total - 1})"
    return md


def extract_spreadsheet(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in CSV_SUFFIXES:
        return _extract_csv(path)
    if suffix in LEGACY_SHEET_SUFFIXES:
        from app.app.services.office_convert import convert_with_soffice

        with tempfile.TemporaryDirectory() as tmp:
            return _extract_xlsx(convert_with_soffice(path, Path(tmp), "xlsx"))
    return _extract_xlsx(path)
