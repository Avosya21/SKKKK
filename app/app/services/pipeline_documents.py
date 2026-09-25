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


def _extract_pdf(path: Path) -> str:
    import pymupdf

    doc = pymupdf.open(path)
    parts: list[str] = []
    table_no = 1
    for page in doc:
        text = page.get_text().strip()
        if len(text) < SCAN_CHARS_THRESHOLD:
            parts.append(_ocr_pdf_page(page) or text)
            continue
        tables = _pdf_page_tables(page)
        if not tables:
            parts.append(text)
            continue
        parts.append(_pdf_page_with_tables(page, tables, table_no))
        table_no += len(tables)
    return "\n\n".join(parts).strip()


def _extract_docx(path: Path) -> str:
    from app.app.services.docx_extract import extract_docx

    return extract_docx(path)


def _extract_image(path: Path, settings: Settings | None = None) -> str:
    import pytesseract
    from PIL import Image, ImageEnhance

    from app.app.services.image_extract import extract_image_via_vision, ocr_text_is_weak
    from app.app.services.tesseract_tools import configure_pytesseract

    tess_cfg = configure_pytesseract()
    img = Image.open(path).convert("L")
    img = ImageEnhance.Contrast(img).enhance(1.5)
    ocr = pytesseract.image_to_string(img, lang="rus+eng", config=tess_cfg).strip()

    if settings is None or settings.dry_run:
        return ocr
    if not ocr_text_is_weak(ocr):
        log_event(logger, "image.ocr_ok", file=path.name, chars=len(ocr))
        return ocr

    log_event(
        logger,
        "image.vision_fallback",
        file=path.name,
        ocr_chars=len(ocr),
        reason="weak_ocr",
    )
    try:
        vision = extract_image_via_vision(settings, path, ocr_hint=ocr)
        log_event(logger, "image.vision_ok", file=path.name, chars=len(vision))
        return vision
    except Exception as exc:
        logger.warning("vision fallback failed for %s: %s", path.name, exc)
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

    if artifact.file_type == "pdf":
        raw = _extract_pdf(path)
    elif artifact.file_type == "docx":
        raw = _extract_docx(path)
    elif artifact.file_type == "xlsx":
        from app.app.services.xlsx_extract import extract_spreadsheet

        raw = extract_spreadsheet(path)
    elif artifact.file_type == "pptx":
        from app.app.services.pptx_extract import extract_pptx

        raw = extract_pptx(path, settings)
    elif artifact.file_type == "txt" or path.suffix.lower() in TEXT_SUFFIXES:
        raw = _extract_plain(path)
    elif artifact.file_type == "html" or path.suffix.lower() in {".html", ".htm"}:
        raw = _extract_html(path)
    elif artifact.file_type == "image":
        raw = _extract_image(path, settings)
    else:
        artifact.error_message = f"формат {path.suffix or artifact.file_type} не поддерживается"
        set_status(db, artifact, "error", reason="unsupported format")
        raise RuntimeError(artifact.error_message)

    if not raw.strip():
        artifact.error_message = "no text extracted"
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

    if settings.dry_run:
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
    set_status(db, artifact, "text_extracted")
    return clean
