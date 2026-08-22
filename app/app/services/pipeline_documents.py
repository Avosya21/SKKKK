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
        return _extract_rtf(path)
    return _extract_text(path)


def _extract_pdf(path: Path) -> str:
    import fitz

    doc = fitz.open(path)
    parts: list[str] = []
    for page in doc:
        text = page.get_text().strip()
        if len(text) >= SCAN_CHARS_THRESHOLD:
            parts.append(text)
            continue
        pix = page.get_pixmap(dpi=200)
        import pytesseract
        from PIL import Image

        from app.app.services.tesseract_tools import configure_pytesseract

        tess_cfg = configure_pytesseract()
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        ocr = pytesseract.image_to_string(img, lang="rus+eng", config=tess_cfg).strip()
        parts.append(ocr or text)
    return "\n\n".join(parts).strip()


def _table_to_markdown(table) -> str:
    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    if not rows:
        return ""
    header = rows[0]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _extract_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts: list[str] = []
    for p in doc.paragraphs:
        if p.text.strip():
            parts.append(p.text.strip())
    for table in doc.tables:
        md = _table_to_markdown(table)
        if md:
            parts.append(md)
    return "\n\n".join(parts).strip()


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
    from lxml import html as lxml_html

    content: str | None = None
    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            content = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        content = path.read_text(encoding="utf-8", errors="replace")

    doc = lxml_html.fromstring(content)
    for el in doc.xpath("//script|//style|//noscript"):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)
    return doc.text_content()


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
    elif artifact.file_type == "txt" or path.suffix.lower() in TEXT_SUFFIXES:
        raw = _extract_plain(path)
    elif artifact.file_type == "html" or path.suffix.lower() in {".html", ".htm"}:
        raw = _extract_html(path)
    elif artifact.file_type == "image":
        raw = _extract_image(path, settings)
    else:
        raw = ""

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
    else:
        try:
            clean = normalize_document(settings, raw)
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
