"""Таблицы документов в базе знаний: DOCX/Excel/CSV/PDF → markdown, нормализация без потерь, нарезка, FTS-поиск.

План: docs/plans/DOCUMENT-TABLES-PLAN.md.
"""

import re
from pathlib import Path

import pytest

from app.app.services.chunking import chunk_document
from app.app.services.table_md import clean_cell, make_table, render_md, split_blocks, table_numbers
from app.app.services.upload import detect_file_type


# --- Шаг 1: формат таблицы ------------------------------------------------------------------------------------


def test_clean_cell_escapes_pipe_and_newlines():
    assert clean_cell("a | b\nвторая  строка") == "a \\| b; вторая строка"
    assert clean_cell("перенос\nпо ширине", soft_wrap=True) == "перенос по ширине"
    assert clean_cell(None) == ""


def test_make_table_drops_blank_rows_cols_and_names_empty_header():
    t = make_table([["", "", ""], ["№", "", "Сумма"], ["1", "", "80 000"]])
    assert t.header == ["№", "Сумма"]
    assert t.rows == [["1", "80 000"]]
    t = make_table([["", "Сумма"], ["x", "1"]])
    assert t.header == ["Столбец 1", "Сумма"]


def test_split_blocks_roundtrip_with_caption_and_headings():
    text = (
        "# 1. Общие сведения\n\nВступление.\n\n"
        + render_md(make_table([["Параметр", "Значение"], ["CRM", "Битрикс24"], ["Пайп", "a \\| b"]]), number=1)
        + "\n\nПосле таблицы."
    )
    blocks = split_blocks(text)
    kinds = [b.kind for b in blocks]
    assert kinds == ["text", "table", "text"]
    table = blocks[1]
    assert table.headings == ["1. Общие сведения"]
    assert table.text.startswith("Таблица 1")
    assert table.table.header == ["Параметр", "Значение"]
    assert table.table.rows[1] == ["Пайп", "a \\| b"]  # экранированная черта не делит ячейку


# --- Шаг 3: типы файлов ----------------------------------------------------------------------------------------


def test_detect_file_type_office_formats():
    assert detect_file_type("smeta.xlsx", None) == "xlsx"
    assert detect_file_type("old.xls", "application/vnd.ms-excel") == "xlsx"
    assert detect_file_type("calc.ods", None) == "xlsx"
    assert detect_file_type("data.csv", "text/csv") == "xlsx"
    assert detect_file_type("data.tsv", None) == "xlsx"
    assert detect_file_type("legacy.doc", "application/msword") == "docx"
    assert detect_file_type("text.odt", None) == "docx"
    assert detect_file_type("noext", "application/pdf") == "pdf"
    assert detect_file_type("archive.zip", None) == "other"


# --- Шаг 2: DOCX ------------------------------------------------------------------------------------------------


def _make_docx(path: Path) -> None:
    from docx import Document

    doc = Document()
    doc.add_paragraph("Техническое задание")
    doc.add_heading("Общие сведения", level=1)
    doc.add_paragraph("Параметры проекта приведены ниже.")
    t = doc.add_table(rows=4, cols=3)
    t.cell(0, 0).text = "Этап"
    t.cell(0, 1).text = "Стоимость"
    t.cell(0, 2).text = "Комментарий"
    t.cell(1, 0).text = "Аналитика"
    t.cell(1, 1).text = "80 000"
    t.cell(1, 2).text = "первая строка\nвторая строка | с чертой"
    t.cell(2, 0).text = "Слой ИИ"
    t.cell(2, 1).text = "240 000"
    t.cell(3, 0).merge(t.cell(3, 1)).text = "Итого 320 000"
    inner = t.cell(2, 2).add_table(rows=1, cols=2)
    inner.cell(0, 0).text = "вложенная"
    inner.cell(0, 1).text = "ячейка"
    doc.add_paragraph("3.2. Модуль «Коннектор к 1С»")
    doc.add_paragraph("Текст после таблицы.")
    doc.sections[0].header.paragraphs[0].text = "ООО Ромашка, конфиденциально"
    doc.save(str(path))


def test_extract_docx_keeps_tables_in_place(tmp_path: Path):
    from app.app.services.docx_extract import extract_docx

    path = tmp_path / "tz.docx"
    _make_docx(path)
    out = extract_docx(path)
    lines = out.splitlines()
    heading = lines.index("# Общие сведения")
    table_start = next(i for i, ln in enumerate(lines) if ln.startswith("| Этап"))
    after = lines.index("Текст после таблицы.")
    assert heading < table_start < after  # таблица под своим разделом, а не в конце файла
    assert "| Аналитика | 80 000 | первая строка; вторая строка \\| с чертой |" in out
    assert "| Итого 320 000 |  |  |" in out  # объединение по горизонтали — одно значение
    assert "вложена в таблицу 1" in out and "| вложенная | ячейка |" in out
    assert "## 3.2. Модуль «Коннектор к 1С»" in out  # нумерованный заголовок без стиля
    assert "## Колонтитулы\nООО Ромашка, конфиденциально" in out
    blocks = split_blocks(out)
    main = next(b for b in blocks if b.kind == "table")
    assert main.headings == ["Общие сведения"]
    assert all(len(r) == 3 for r in main.table.rows)


def test_extract_legacy_doc_via_libreoffice(tmp_path: Path):
    from app.app.services.docx_extract import extract_docx
    from app.app.services.office_convert import convert_with_soffice, find_soffice

    if not find_soffice():
        pytest.skip("LibreOffice не установлен")
    src = tmp_path / "tz.docx"
    _make_docx(src)
    out_dir = tmp_path / "doc"
    out_dir.mkdir()
    doc = convert_with_soffice(src, out_dir, "doc")
    out = extract_docx(doc)
    assert "80 000" in out and "| Этап | Стоимость | Комментарий |" in out


# --- Шаг 3: Excel / CSV -----------------------------------------------------------------------------------------


def _make_xlsx(path: Path) -> None:
    from datetime import datetime

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Смета"
    ws["A1"] = "Смета проекта"
    ws.merge_cells("A1:E1")
    ws["A3"] = "№"
    ws.merge_cells("A3:A4")
    ws["B3"] = "Этап работ"
    ws.merge_cells("B3:B4")
    ws["C3"] = "Стоимость"
    ws.merge_cells("C3:D3")
    ws["E3"] = "Срок"
    ws.merge_cells("E3:E4")
    ws["C4"] = "без НДС"
    ws["D4"] = "с НДС"
    for i, (n, name, cost, vat, due) in enumerate(
        [(1, "Аналитика", 80000, 97600, datetime(2026, 10, 1)), (2, "Слой ИИ / MCP", 240000, 292800, datetime(2026, 11, 15))],
        start=5,
    ):
        ws.cell(i, 1, n)
        ws.cell(i, 2, name)
        ws.cell(i, 3, cost).number_format = "#,##0"
        ws.cell(i, 4, vat).number_format = "#,##0"
        ws.cell(i, 5, due)
    ws["B7"] = "Итого"
    ws["C7"] = 320000
    ws["C7"].number_format = "#,##0"
    ws["F8"] = 0.25
    ws["F8"].number_format = "0%"
    ws["A10"] = "Контакты"
    ws["A11"] = "Имя"
    ws["B11"] = "Роль"
    ws["A12"] = "Иван"
    ws["B12"] = "РОП"
    ws["Z500"].number_format = "0"  # пустая отформатированная ячейка не раздувает лист
    hidden = wb.create_sheet("Скрытый")
    hidden["A1"] = "секрет"
    hidden.sheet_state = "hidden"
    wb.save(str(path))


def test_extract_xlsx_tables_with_merged_header(tmp_path: Path):
    from app.app.services.xlsx_extract import extract_spreadsheet

    path = tmp_path / "smeta.xlsx"
    _make_xlsx(path)
    out = extract_spreadsheet(path)
    assert out.startswith("## Лист: Смета")
    assert "Таблица 1: Смета проекта" in out
    assert "| № | Этап работ | Стоимость / без НДС | Стоимость / с НДС | Срок |" in out
    assert "| 2 | Слой ИИ / MCP | 240 000 | 292 800 | 15.11.2026 |" in out
    assert "| Итого | 320 000 |" in out
    assert "25%" in out
    assert "| Имя | Роль |" in out and "| Иван | РОП |" in out
    assert "секрет" not in out
    assert "показаны строки" not in out


def test_extract_csv_semicolon_cp1251(tmp_path: Path):
    from app.app.services.xlsx_extract import extract_spreadsheet

    path = tmp_path / "plan.csv"
    path.write_bytes("Менеджер;План, руб\nИванов;800 000\nПетрова;1 100 000\n".encode("cp1251"))
    out = extract_spreadsheet(path)
    assert "| Менеджер | План, руб |" in out
    assert "| Петрова | 1 100 000 |" in out


def test_process_document_spreadsheet_skips_llm(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    from app.app.services import pipeline_documents as pd

    path = tmp_path / "smeta.xlsx"
    _make_xlsx(path)
    monkeypatch.setattr(pd, "set_status", lambda *a, **k: None)
    monkeypatch.setattr(pd, "parsed_dir", lambda *_a: tmp_path / "parsed")
    monkeypatch.setattr(pd, "normalize_document", lambda *_a: pytest.fail("LLM для Excel не вызывается"))
    art = SimpleNamespace(file_path=str(path), file_type="xlsx", artifact_id="art_x", project_id=1,
                          source_type="document", error_message=None, transcription_file_path=None,
                          transcription_text=None)
    clean = pd.process_document_artifact(None, SimpleNamespace(dry_run=False), art)
    assert "| 1 | Аналитика | 80 000 | 97 600 | 01.10.2026 |" in clean


# --- Шаг 4: PDF -------------------------------------------------------------------------------------------------


def _make_pdf_table(path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), "Estimate for the project")
    rows = [["No", "Stage", "Cost"], ["1", "Analysis", "80 000"], ["2", "AI layer", "240 000"], ["", "Total", "320 000"]]
    x = [50, 100, 300, 420]
    y0, h = 80, 22
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            rect = pymupdf.Rect(x[c], y0 + r * h, x[c + 1], y0 + (r + 1) * h)
            page.draw_rect(rect, color=(0, 0, 0), width=0.8)
            if val:
                page.insert_text((rect.x0 + 4, rect.y1 - 6), val, fontsize=10)
    page.insert_text((50, y0 + len(rows) * h + 30), "Notes after the table")
    doc.save(str(path))


def test_extract_pdf_table_keeps_columns(tmp_path: Path):
    from app.app.services.pipeline_documents import _extract_pdf

    path = tmp_path / "estimate.pdf"
    _make_pdf_table(path)
    out = _extract_pdf(path)
    assert "| No | Stage | Cost |" in out
    assert "| 2 | AI layer | 240 000 |" in out
    assert out.index("Estimate for the project") < out.index("| No |") < out.index("Notes after the table")
    assert out.count("Analysis") == 1  # текст таблицы не дублируется простым текстом страницы


# --- HTML (выгрузка Google Sheets / Excel как веб-страницы, обычные страницы) -------------------------------------


def test_extract_html_sheet_export_tables(tmp_path: Path):
    from app.app.services.pipeline_documents import _extract_html

    ruler = "<tr><th></th>" + "".join(f"<th>{c}</th>" for c in "ABCDE") + "</tr>"
    rows = [
        ["", "", "", "", ""],
        ["ПЛАН", "2 000", "", "", ""],
        ["Менеджер", '<td colspan="2">апр.-25</td>', '<td colspan="2">мая-25</td>'],
        ["Клиент", "Вес", "Прибыль", "Вес", "Прибыль"],
        ["Бацких Федор", "952", "99 272", "875", "42 877"],
        ["ВИАНТИКА-МЕХ", "", "", "354", "35 752"],
    ]
    body = ""
    for i, row in enumerate(rows, start=1):
        cells = "".join(c if c.startswith("<td") else f"<td>{c}</td>" for c in row)
        body += f"<tr><th>{i}</th>{cells}</tr>"
    path = tmp_path / "sheet.html"
    path.write_text(f"<html><body><p>Прогноз</p><table>{ruler}{body}</table><div>после</div><div>таблицы</div></body></html>",
                    encoding="utf-8")
    out = _extract_html(path)
    assert "ABCDE" not in out.replace(" ", "") and "| A |" not in out  # линейка столбцов убрана
    assert "Таблица 1: ПЛАН; 2 000" in out
    assert "| Менеджер / Клиент | апр.-25 / Вес | апр.-25 / Прибыль | мая-25 / Вес | мая-25 / Прибыль |" in out
    assert "| Бацких Федор | 952 | 99 272 | 875 | 42 877 |" in out
    assert out.index("Прогноз") < out.index("Таблица 1") < out.index("после\nтаблицы")  # блоки не склеены


def test_extract_html_rowspan_and_layout_text(tmp_path: Path):
    from app.app.services.pipeline_documents import _extract_html

    path = tmp_path / "page.html"
    path.write_text(
        "<html><body><h1>Смета</h1><div>Первый блок</div><div>Второй блок</div>"
        "<table><tr><th>Этап</th><th>Сумма</th></tr>"
        '<tr><td rowspan="2">Аналитика</td><td>80 000</td></tr><tr><td>20 000</td></tr></table>'
        "<table><tr><td>одна ячейка вёрстки</td></tr></table></body></html>",
        encoding="utf-8",
    )
    out = _extract_html(path)
    assert "Первый блок\nВторой блок" in out
    assert "| Аналитика | 20 000 |" in out  # rowspan — значение повторяется в строке ниже
    assert "одна ячейка вёрстки" in out and "| одна ячейка" not in out  # таблица вёрстки осталась текстом


def test_normalize_invented_token_does_not_lose_text(monkeypatch):
    """Модель вернула только метку, которой во входе не было: текст берётся без LLM, метка не попадает в итог."""
    from app.app.services import normalize

    monkeypatch.setattr(normalize, "chat_completion_no_think", lambda *_a, **_k: "[[ТАБЛИЦА_1]]")
    raw = "Прогноз продаж по менеджерам на квартал. " * 30
    clean = normalize.normalize_document(None, raw)
    assert "[[ТАБЛИЦА" not in clean and "Прогноз продаж" in clean


# --- Шаг 5: нормализация ----------------------------------------------------------------------------------------


def _doc_with_tables() -> str:
    t1 = render_md(make_table([["Этап", "Сумма"], ["Аналитика", "80 000"], ["ИИ", "240 000"]]), number=1)
    t2 = render_md(make_table([["Имя", "Роль"], ["Иван", "РОП"]]), number=2)
    return f"# Смета\n\nСтраница 1\nВводный   текст про смету.\n\n{t1}\n\nПромежуточный текст.\n\n{t2}\n\nКонец."


def test_normalize_keeps_tables_verbatim(monkeypatch):
    from app.app.services import normalize

    seen: list[str] = []

    def fake_llm(_settings, *, system, user, max_tokens):
        seen.append(user)
        assert "| Аналитика |" not in user  # таблицы в LLM не уходят
        return user.split("\n\n", 1)[1].replace("Страница 1\n", "").replace("   ", " ")

    monkeypatch.setattr(normalize, "chat_completion_no_think", fake_llm)
    raw = _doc_with_tables()
    clean = normalize.normalize_document(None, raw)
    assert "[[ТАБЛИЦА_" not in clean
    assert table_numbers(clean) == table_numbers(raw)
    assert clean.index("| Аналитика |") < clean.index("Промежуточный текст.") < clean.index("| Иван | РОП |")
    assert "Страница 1" not in clean and seen


def test_normalize_restores_lost_table_token_and_guards_shrink(monkeypatch):
    from app.app.services import normalize

    monkeypatch.setattr(normalize, "chat_completion_no_think", lambda *_a, **_k: "Кратко.")
    raw = _doc_with_tables() + "\n\n" + "Длинный абзац с фактами и датой 19.08.2026. " * 30
    clean = normalize.normalize_document(None, raw)
    assert table_numbers(clean) == table_numbers(raw)  # метки потеряны LLM — таблицы всё равно на месте
    assert "19.08.2026" in clean  # ответ «ужат» — взята лёгкая нормализация


def test_normalize_whitespace_cleanup_is_not_shrink(monkeypatch):
    """Отступы HTML убираются законно: ответ без них не считается «ужатым»."""
    from app.app.services import normalize

    monkeypatch.setattr(normalize, "chat_completion_no_think",
                        lambda _s, *, system, user, max_tokens: " ".join(user.split("\n\n", 1)[1].split()) + " ОЧИЩЕНО")
    raw = "\n".join("        " + f"Сообщение {i}: договорились на вторник в 16.00" + "          " for i in range(40))
    clean = normalize.normalize_document(None, raw)
    assert "ОЧИЩЕНО" in clean  # взят ответ модели, а не лёгкая нормализация


def test_normalize_rejects_answer_that_drops_numbers(monkeypatch):
    """Модель выбросила таймкоды реплик — часть берётся без LLM, числа на месте; остальные ответы модели принимаются."""
    from app.app.services import normalize

    def drop_timecodes(_s, *, system, user, max_tokens):
        body = user.split("\n\n", 1)[1]
        return re.sub(r"\[\d{2}:\d{2}:\d{2}\.\d{3} - \d{2}:\d{2}:\d{2}\.\d{3}\] ", "", body)

    monkeypatch.setattr(normalize, "chat_completion_no_think", drop_timecodes)
    raw = "\n".join(f"[00:{i:02d}:00.000 - 00:{i:02d}:30.500] Реплика номер слово слово слово" for i in range(40))
    clean = normalize.normalize_document(None, raw)
    assert "[00:39:00.000 - 00:39:30.500]" in clean

    monkeypatch.setattr(normalize, "chat_completion_no_think",
                        lambda _s, *, system, user, max_tokens: user.split("\n\n", 1)[1].replace("Страница 1\n", "") + " ОК")
    clean = normalize.normalize_document(None, "Страница 1\n" + "Сумма 240 000 руб., срок 19.08.2026. " * 30)
    assert clean.rstrip().endswith("ОК")  # одна потерянная цифра номера страницы — не повод отбрасывать ответ


def test_normalize_splits_long_text_without_headings(monkeypatch):
    from app.app.services import normalize

    calls: list[int] = []

    def fake_llm(_settings, *, system, user, max_tokens):
        calls.append(len(user))
        return user.split("\n\n", 1)[1]

    monkeypatch.setattr(normalize, "chat_completion_no_think", fake_llm)
    raw = "\n\n".join(f"Абзац {i}. " + "текст " * 150 for i in range(60))  # ~55 тыс. символов, заголовков нет
    clean = normalize.normalize_document(None, raw)
    assert len(calls) >= 6
    assert max(calls) <= normalize.SECTION_MAX_CHARS + 200
    assert "Абзац 59." in clean and len(clean) >= 0.95 * len(raw)


# --- Шаг 6: нарезка ---------------------------------------------------------------------------------------------


def test_chunk_document_big_table_repeats_header_and_keeps_rows():
    grid = [["№", "Этап", "Сумма"]] + [[str(i), f"Этап номер {i}", f"{i * 1000}"] for i in range(1, 301)]
    text = "# Раздел сметы\n\nВступление.\n\n" + render_md(make_table(grid), number=1) + "\n\nЗаключение."
    chunks = chunk_document(text)
    table_chunks = [c for c in chunks if "| --- |" in c.text]
    assert len(table_chunks) > 1
    rows = 0
    for c in table_chunks:
        lines = c.text.splitlines()
        assert lines[0] == "Раздел: Раздел сметы"
        assert lines[1] == "Таблица 1"
        assert "| № | Этап | Сумма |" in lines
        rows += sum(1 for ln in lines if ln.startswith("| ") and not ln.startswith(("| № ", "| --- ")))
        assert all(ln.endswith("|") for ln in lines if ln.startswith("|"))  # строки не разрезаны
        assert len(c.text) <= 512 * 4
    assert rows == 300
    assert any(c.text.endswith("Заключение.") for c in chunks)


def test_chunk_document_wide_table_as_records():
    header = [f"Кол{j}" for j in range(12)]
    grid = [header] + [[f"r{i}c{j}" for j in range(12)] for i in range(3)]
    chunks = chunk_document(render_md(make_table(grid), number=1))
    assert len(chunks) == 1
    assert "Строка 2: Кол0: r1c0; Кол1: r1c1" in chunks[0].text


def test_chunk_document_prose_keeps_lines_and_section():
    text = "# Требования\n\n" + "\n\n".join(f"Пункт {i}. " + "слово " * 60 for i in range(20))
    chunks = chunk_document(text)
    assert len(chunks) > 1
    assert chunks[0].text.startswith("# Требования\nПункт 0.")
    assert chunks[1].text.startswith("Раздел: Требования\n")
    joined = "\n".join(c.text for c in chunks)
    assert all(f"Пункт {i}." in joined for i in range(20))


def test_chunk_document_keeps_sections_before_table():
    """Разделы «заголовок + текст» подряд перед таблицей не теряются: убирается только голый заголовок таблицы."""
    table = render_md(make_table([["Объект", "Количество"], ["Партнёры", "20 816"]]), number=1)
    text = (
        "## Резюме\n\n### AS-IS\nУчёт ведётся в 1С.\n\n### Этап 2\nПолная синхронизация клиентов.\n\n"
        f"## Объёмы данных\n\n{table}\n\nПосле таблицы."
    )
    chunks = chunk_document(text)
    joined = "\n".join(c.text for c in chunks)
    assert "Учёт ведётся в 1С." in joined
    assert "Полная синхронизация клиентов." in joined
    assert "## Объёмы данных" not in joined  # голый заголовок — в строке «Раздел: …» таблицы
    assert any(c.text.startswith("Раздел: Объёмы данных") and "| Партнёры | 20 816 |" in c.text for c in chunks)
    assert chunks[-1].text.endswith("После таблицы.")


# --- Шаг 7: полнотекстовый поиск --------------------------------------------------------------------------------


def test_fts_query_and_number_normalization():
    from app.app.services.chunk_fts import build_match_query, fts_normalize

    assert fts_normalize("итого 1 104 000 руб, 240 000") == "итого 1104000 руб, 240000"
    q = build_match_query("Сколько стоит слой ИИ 240 000?")
    assert '"240000"' in q and '"стои"*' in q and "сколько" not in q.lower()


def test_index_and_search_table_row_by_number(tmp_data, db_engine, client, admin_token, monkeypatch):
    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services.chunk_fts import search_chunk_fts
    from app.app.services.indexing import index_artifact
    from app.app.services.retrieval import fuse_chunk_hits

    monkeypatch.setattr("app.app.services.indexing.embed_texts", lambda _s, texts: [[0.01] * 1024 for _ in texts])

    db = SessionLocal()
    user = db.scalar(select(User).limit(1))
    project = Project(name="FtsTables", description="", created_by=user.id, is_active=True)
    db.add(project)
    db.commit()
    art = Artifact(artifact_id="art_fts_tab", project_id=project.id, user_id=user.id, original_filename="смета.xlsx",
                   stored_filename="s.xlsx", file_path="s.xlsx", file_type="xlsx", source_type="document",
                   status="done", sha256="fts1")
    db.add(art)
    db.commit()
    text = "## Лист: Смета\n\n" + render_md(
        make_table([["Этап работ", "Стоимость"], ["Аналитика", "80 000"], ["Слой ИИ / MCP (Умный прогнозист)", "240 000"]]),
        number=1,
    )
    assert index_artifact(db, get_settings(), art, text) == 1
    db.commit()

    hits = search_chunk_fts(db, "сколько стоит прогнозист 240000", project_id=project.id)
    assert hits and "| Слой ИИ / MCP (Умный прогнозист) | 240 000 |" in hits[0]["text"]
    assert hits[0]["text"].startswith("Раздел: Лист: Смета\nТаблица 1")

    fused = fuse_chunk_hits(db, "прогнозист", [0.01] * 1024, [], project_id=project.id, limit=5, min_score=1.5)
    assert [h["chunk_id"] for h in fused] == [hits[0]["chunk_id"]]  # находка по слову проходит ниже порога

    index_artifact(db, get_settings(), art, "## Лист: Смета\n\nпусто")  # переиндексация чистит старые строки FTS
    db.commit()
    assert search_chunk_fts(db, "прогнозист", project_id=project.id) == []
    db.close()
