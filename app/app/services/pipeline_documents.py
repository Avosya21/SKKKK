"""Извлечение текста из документов и изображений: TXT, RTF, PDF, DOCX, HTML, таблицы в Markdown, OCR."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.services.normalize import light_normalize, normalize_document
from app.app.services.status import set_status
from app.app.services.storage import parsed_dir
from app.app.services.table_md import Table, clean_cell, make_table, render_md

logger = logging.getLogger("skkkk.documents")
SCAN_CHARS_THRESHOLD = 40
PDF_FIGURE_MIN_AREA = 0.08  # доля площади страницы
PDF_FIGURE_MIN_SIDE_PT = 120
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rtf", ".csv", ".log", ".json", ".yaml", ".yml", ".ini", ".text"}
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251", "cp866", "latin-1")


def _decode_text_bytes(data: bytes) -> str:
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16-le")
    if data.startswith(b"\xfe\xff"):
        return data.decode("utf-16-be")
    for encoding in TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_text(path: Path) -> str:
    data = path.read_bytes()
    if not data.strip():
        return ""
    text = _decode_text_bytes(data)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _extract_rtf(path: Path) -> str:
    raw = path.read_bytes().decode("latin-1", errors="replace")

    def _hex_char(match: re.Match[str]) -> str:
        try:
            return bytes.fromhex(match.group(1)).decode("cp1251", errors="replace")
        except ValueError:
            return ""

    text = re.sub(r"\\'([0-9a-fA-F]{2})", _hex_char, raw)
    text = re.sub(r"\\par\b", "\n", text)
    text = re.sub(r"\\line\b", "\n", text)
    text = re.sub(r"\\tab\b", "\t", text)
    text = re.sub(r"\\u(-?\d+)\?", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = re.sub(r"[{}]", "", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _extract_plain(path: Path) -> str:
    if path.suffix.lower() == ".rtf":
        # RTF с таблицами: через LibreOffice → DOCX (таблицы на местах); без LibreOffice — разбор текста регулярками.
        from app.app.services.office_convert import find_soffice

        if find_soffice():
            try:
                from app.app.services.docx_extract import extract_docx

                out = extract_docx(path)
                if out.strip():
                    return out
            except Exception as exc:
                logger.warning("rtf via libreoffice failed for %s: %s", path.name, exc)
        return _extract_rtf(path)
    return _extract_text(path)


def _ocr_pdf_page(page) -> str:
    import pytesseract
    from PIL import Image

    from app.app.services.tesseract_tools import configure_pytesseract

    pix = page.get_pixmap(dpi=200)
    tess_cfg = configure_pytesseract()
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    return pytesseract.image_to_string(img, lang="rus+eng", config=tess_cfg).strip()


def _pdf_page_tables(page) -> list[tuple[tuple[float, float, float, float], Table]]:
    """Таблицы страницы (PyMuPDF find_tables): только настоящие — от 2 столбцов и хотя бы одной строки данных."""
    try:
        found = page.find_tables().tables
    except Exception as exc:  # битая страница — остаёмся на тексте
        logger.warning("pdf find_tables failed: %s", exc)
        return []
    out = []
    for t in found:
        grid = [[clean_cell(c, soft_wrap=True) for c in row] for row in t.extract()]
        table = make_table(grid)
        if table is None or not table.rows or len(table.header) < 2:
            continue
        out.append((tuple(t.bbox), table))
    return out


def _pdf_page_with_tables(page, tables, first_no: int) -> str:
    """Текст страницы без областей таблиц + таблицы markdown на своих местах (по вертикали)."""
    import pymupdf

    rects = [pymupdf.Rect(bbox) for bbox, _t in tables]
    items: list[tuple[float, int, str]] = []
    for order, block in enumerate(page.get_text("blocks")):
        x0, y0, x1, y1, text, _no, btype = block[:7]
        if btype != 0 or not text.strip():
            continue
        r = pymupdf.Rect(x0, y0, x1, y1)
        if any(r.intersects(tr) and (r & tr).get_area() >= 0.5 * max(r.get_area(), 1e-6) for tr in rects):
            continue
        items.append((y0, order, text.strip()))
    for k, (bbox, table) in enumerate(tables):
        items.append((bbox[1], -1, render_md(table, number=first_no + k)))
    items.sort(key=lambda it: (it[0], it[1]))
    return "\n\n".join(text for _y, _o, text in items)


def _pdf_figure_rects(page) -> list:
    """Крупные картинки на странице с текстом (схемы процессов, скриншоты): от 8% площади и от 120 pt по меньшей
    стороне — иконки и логотипы не берём; картинки, лежащие внутри уже взятой, не повторяем."""
    page_area = page.rect.get_area()
    rects: list = []
    for img in page.get_images(full=True):
        try:
            found = page.get_image_rects(img[0])
        except Exception:
            continue
        for r in found:
            r = r & page.rect
            if r.is_empty or r.get_area() < PDF_FIGURE_MIN_AREA * page_area or min(r.width, r.height) < PDF_FIGURE_MIN_SIDE_PT:
                continue
            if any((r & x).get_area() >= 0.8 * r.get_area() for x in rects if r.intersects(x)):
                continue
            rects.append(r)
    return sorted(rects, key=lambda r: (r.y0, r.x0))


def _read_pdf_image(page, settings: Settings, clip=None) -> str:
    """Страница-скан (``clip=None``) или картинка страницы → текст через ``_extract_image``: vision-модель
    с черновиком OCR, при её сбое — OCR. Ошибка не роняет разбор документа: пустая строка."""
    import tempfile

    pix = page.get_pixmap(dpi=200, clip=clip)
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / f"page{page.number + 1}.png"
        pix.save(str(img))
        try:
            return _extract_image(img, settings).strip()
        except Exception as exc:
            logger.warning("pdf image read failed (page %s): %s", page.number + 1, exc)
            return ""


def _extract_pdf(path: Path, settings: Settings | None = None) -> str:
    """Текст страниц + таблицы на местах. В живом прогоне страницы-сканы и крупные картинки на страницах с текстом
    (схемы, скриншоты) читает vision-модель — без лимита: полнота важнее времени. Без настроек и в сухом
    прогоне — только OCR сканов, картинки не читаются."""
    import pymupdf

    doc = pymupdf.open(path)
    parts: list[str] = []
    table_no = 1
    live = settings is not None and not settings.dry_run
    scans = figures = 0
    for page in doc:
        text = page.get_text().strip()
        if len(text) < SCAN_CHARS_THRESHOLD:
            scans += 1
            parts.append((_read_pdf_image(page, settings) if live else _ocr_pdf_page(page)) or text)
            continue
        tables = _pdf_page_tables(page)
        if not tables:
            page_md = text
        else:
            page_md = _pdf_page_with_tables(page, tables, table_no)
            table_no += len(tables)
        for rect in _pdf_figure_rects(page) if live else []:
            fig = _read_pdf_image(page, settings, clip=rect)
            if fig:
                figures += 1
                page_md += f"\n\nРисунок на странице {page.number + 1}:\n{fig}"
        parts.append(page_md)
    if scans or figures:
        log_event(logger, "pdf.images_read", file=path.name, scans=scans, figures=figures, vision=live)
    return "\n\n".join(parts).strip()


def _extract_docx(path: Path, settings: Settings | None = None) -> str:
    from app.app.services.docx_extract import extract_docx

    return extract_docx(path, settings)


def _extract_image(path: Path, settings: Settings | None = None) -> str:
    """Картинка → текст. В живом прогоне всегда vision-модель (OCR схем и форм — мусор, и проверка «мусора»
    ловит не всё), черновик Tesseract идёт ей подсказкой; при сбое модели остаётся OCR."""
    import pytesseract
    from PIL import Image, ImageEnhance

    from app.app.services.image_extract import extract_image_via_vision
    from app.app.services.tesseract_tools import configure_pytesseract

    tess_cfg = configure_pytesseract()
    img = Image.open(path).convert("L")
    img = ImageEnhance.Contrast(img).enhance(1.5)
    ocr = pytesseract.image_to_string(img, lang="rus+eng", config=tess_cfg).strip()

    if settings is None or settings.dry_run:
        return ocr
    try:
        vision = extract_image_via_vision(settings, path, ocr_hint=ocr)
        log_event(logger, "image.vision_ok", file=path.name, chars=len(vision), ocr_chars=len(ocr))
        return vision
    except Exception as exc:
        logger.warning("vision failed for %s: %s", path.name, exc)
        if ocr.strip():
            log_event(logger, "image.vision_failed_keep_ocr", file=path.name, error=str(exc))
            return ocr
        raise


def _extract_html(path: Path) -> str:
    from app.app.services.html_extract import extract_html

    return extract_html(path)


def _strip_page_numbers(text: str) -> str:
    return re.sub(r"(?m)^\s*\d+\s*$", "", text)


SHORT_TXT_NORMALIZE_CHARS = 3000


def process_document_artifact(db: Session, settings: Settings, artifact: Artifact) -> str:
    path = Path(artifact.file_path)
    set_status(db, artifact, "preprocessing")
    is_chat = False  # переписка (выгрузка Telegram, копия чата Битрикс24): текст — сообщения дословно

    if artifact.file_type == "pdf":
        raw = _extract_pdf(path, settings)
    elif artifact.file_type == "docx":
        raw = _extract_docx(path, settings)
    elif artifact.file_type == "xlsx":
        from app.app.services.xlsx_extract import extract_spreadsheet

        raw = extract_spreadsheet(path)
    elif artifact.file_type == "pptx":
        from app.app.services.pptx_extract import extract_pptx

        raw = extract_pptx(path, settings)
    elif artifact.file_type == "txt" or path.suffix.lower() in TEXT_SUFFIXES:
        from app.app.services.chat_export import ingest_bitrix_copy

        raw = _extract_plain(path)
        # Копия чата Битрикс24: сообщения с автором и ответами (время неизвестно — только день материала)
        chat_text = ingest_bitrix_copy(settings, artifact, raw)
        if chat_text is not None:
            raw, is_chat = chat_text, True
    elif artifact.file_type == "html" or path.suffix.lower() in {".html", ".htm"}:
        from app.app.services.chat_export import ingest_telegram_export

        # Выгрузка Telegram: сообщения с автором и временем из разметки (SOURCE-ANCHORS-PLAN этап 1), не «текст страницы»
        chat_text = ingest_telegram_export(settings, artifact, _extract_text(path))
        raw = chat_text if chat_text is not None else _extract_html(path)
        is_chat = chat_text is not None
    elif artifact.file_type == "image":
        raw = _extract_image(path, settings)
    else:
        artifact.error_message = f"формат {path.suffix or artifact.file_type} не поддерживается"
        set_status(db, artifact, "error", reason="unsupported format")
        raise RuntimeError(artifact.error_message)

    if not raw.strip():
        if artifact.file_type == "html" or path.suffix.lower() in {".html", ".htm"}:
            # Страница, которую собирает скрипт в браузере (в файле только <div id="root">): текста в разметке нет
            artifact.error_message = ("в файле нет текста: страницу, вероятно, собирает скрипт в браузере — "
                                      "откройте её и сохраните как PDF (Печать → Сохранить как PDF), затем загрузите PDF")
        else:
            artifact.error_message = "в файле не найден текст — проверьте, что файл открывается и в нём есть текст"
        set_status(db, artifact, "error", reason="no text extracted")
        raise RuntimeError("no text extracted")

    log_event(logger, "document.extracted", artifact=artifact.artifact_id, file_type=artifact.file_type, chars=len(raw))
    raw = _strip_page_numbers(raw)
    out_dir = parsed_dir(settings, artifact.project_id, artifact.source_type)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = artifact.artifact_id
    raw_path = out_dir / f"{base}_raw.txt"
    clean_path = out_dir / f"{base}_clean.txt"
    raw_path.write_text(raw, encoding="utf-8")

    if is_chat or getattr(artifact, "recorded_at_source", None) == "messages":
        clean = raw  # переписка: сообщения дословно — модель их не переписывает (цитаты и авторы должны совпадать)
        mode = "chat export"
    elif settings.dry_run:
        clean = light_normalize(raw)
        mode = "light (dry_run)"
    elif artifact.file_type == "txt" and len(raw) <= SHORT_TXT_NORMALIZE_CHARS:
        clean = light_normalize(raw)
        mode = "light (short txt)"
    elif artifact.file_type == "xlsx":
        clean = light_normalize(raw)  # только таблицы: LLM их не чистит (цифры переносятся дословно)
        mode = "light (spreadsheet)"
    else:
        try:
            clean = normalize_document(settings, raw, source=artifact.artifact_id)
            mode = "llm"
        except Exception as exc:
            logger.warning("normalization failed, using light: %s", exc)
            clean = light_normalize(raw)
            mode = "light (llm failed)"

    log_event(
        logger,
        "document.normalized",
        artifact=artifact.artifact_id,
        mode=mode,
        chars=len(clean),
        file=clean_path.name,
    )
    clean_path.write_text(clean, encoding="utf-8")
    artifact.transcription_file_path = str(clean_path)
    artifact.transcription_text = clean
    from app.app.services.recorded_at import refine_from_text

    # Дата документа из его шапки («Протокол от 19.08.2026») главнее свойств файла и даты загрузки;
    # у переписки дата внутри сообщения — не дата переписки
    if not is_chat and refine_from_text(artifact, raw):
        log_event(logger, "document.dated", artifact=artifact.artifact_id, source=artifact.recorded_at_source,
                  day=artifact.recorded_at.date().isoformat())
    set_status(db, artifact, "text_extracted")
    return clean
