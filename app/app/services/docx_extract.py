"""DOCX → markdown в порядке документа: заголовки (#), абзацы, списки, таблицы на своих местах, надписи, колонтитулы.

Таблица стоит под своим разделом (раньше все таблицы шли в конец файла и теряли контекст). Объединённые по
горизонтали ячейки дают одно значение и пустые соседние, по вертикали — повтор значения (строка самодостаточна).
Вложенные таблицы — отдельным блоком сразу после родительской. Старые .doc/.odt/.rtf — через LibreOffice.
Картинки (схемы, скриншоты) в живом прогоне читает vision-модель: «Рисунок N:» сразу после абзаца или таблицы, где
картинка стоит; повтор той же картинки не читается заново; иконки меньше 200 px пропускаются; EMF/WMF (Visio) —
через LibreOffice в PNG.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

from app.app.services.table_md import CAPTION_PREFIX, clean_cell, make_table, render_md

logger = logging.getLogger("skkkk.docx")

LEGACY_WORD_SUFFIXES = {".doc", ".docm", ".odt", ".rtf", ".dot", ".dotx"}
_STYLE_HEADING = re.compile(r"(?:heading|заголовок)\s*(\d)", re.I)
_NUMBERED_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+[А-ЯЁA-Z«\"(]")
NUMBERED_HEADING_MAX_CHARS = 120
IMAGE_MIN_SIDE_PX = 200  # меньше — иконка/логотип
VECTOR_IMAGE_SUFFIXES = {".emf", ".wmf"}


def _paragraph_heading_level(par) -> int:
    try:
        name = par.style.name if par.style is not None else ""
    except Exception:
        name = ""
    m = _STYLE_HEADING.search(name or "")
    if m:
        return max(1, min(6, int(m.group(1))))
    if (name or "").lower() in {"title", "название"}:
        return 1
    ppr = par._p.pPr
    if ppr is not None:
        lvl = ppr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}outlineLvl")
        if lvl is not None:
            val = lvl.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val")
            if val and val.isdigit() and int(val) < 9:
                return min(6, int(val) + 1)
    return 0


def _numbered_heading_level(text: str, is_list: bool) -> int:
    """«1. Общие сведения», «3.2. Модуль …» без стиля заголовка: короткая строка без точки в конце."""
    if is_list or len(text) > NUMBERED_HEADING_MAX_CHARS or text.endswith((".", ";", ",", ":")):
        return 0
    m = _NUMBERED_HEADING.match(text)
    if not m:
        return 0
    return min(4, m.group(1).count(".") + 1)


def _is_list_paragraph(par) -> bool:
    ppr = par._p.pPr
    return ppr is not None and ppr.numPr is not None


def _textbox_texts(element) -> list[str]:
    out: list[str] = []
    for box in element.xpath(".//w:txbxContent"):
        for p in box.xpath(".//w:p"):
            text = "".join(t.text or "" for t in p.xpath(".//w:t")).strip()
            if text:
                out.append(text)
    return out


def _cell_text(cell) -> str:
    return "\n".join(p.text for p in cell.paragraphs)


def _row_cells(row) -> list:
    try:
        return list(row.cells)
    except Exception:  # битая сетка таблицы: берём ячейки как есть
        from docx.table import _Cell

        return [_Cell(tc, row) for tc in row._tr.tc_lst]


def _table_grid(table) -> tuple[list[list[str]], list]:
    grid: list[list[str]] = []
    nested: list = []
    seen_nested: set[int] = set()
    for row in table.rows:
        out: list[str] = []
        prev = None
        for cell in _row_cells(row):
            if prev is not None and cell._tc is prev:
                out.append("")  # горизонтальное объединение: значение только в первой ячейке
                continue
            prev = cell._tc
            out.append(clean_cell(_cell_text(cell)))
            for inner in cell.tables:
                if id(inner._tbl) not in seen_nested:
                    seen_nested.add(id(inner._tbl))
                    nested.append(inner)
        grid.append(out)
    return grid, nested


def _image_text(blob: bytes, suffix: str, settings) -> str:
    """Картинка из документа → текст (vision-модель с подсказкой OCR); мелкие и нечитаемые — пустая строка."""
    from PIL import Image

    from app.app.services.pipeline_documents import _extract_image

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"image{suffix}"
        src.write_bytes(blob)
        if suffix in VECTOR_IMAGE_SUFFIXES:
            from app.app.services.office_convert import convert_with_soffice

            src = convert_with_soffice(src, Path(tmp), "png")
        with Image.open(src) as img:
            if min(img.size) < IMAGE_MIN_SIDE_PX:
                return ""
        return _extract_image(src, settings).strip()


class _Emitter:
    def __init__(self, doc=None, settings=None) -> None:
        self.parts: list[str] = []
        self.table_no = 0
        self.doc = doc
        self.read_images = settings is not None and not getattr(settings, "dry_run", True)
        self.settings = settings
        self.figure_no = 0
        self.seen_images: set[str] = set()

    def images(self, element) -> None:
        """Картинки внутри элемента (абзац, таблица) — блоками «Рисунок N:» на этом месте."""
        if not self.read_images:
            return
        rids = element.xpath(".//*[local-name()='blip']/@*[local-name()='embed']")
        rids += element.xpath(".//*[local-name()='imagedata']/@*[local-name()='id']")
        for rid in rids:
            part = self.doc.part.related_parts.get(rid)
            name = str(getattr(part, "partname", "")) if part is not None else ""
            if not name or name in self.seen_images:
                continue
            self.seen_images.add(name)
            try:
                text = _image_text(part.blob, Path(name).suffix.lower(), self.settings)
            except Exception as exc:
                logger.warning("docx image %s not read: %s", name, exc)
                continue
            if text:
                self.figure_no += 1
                self.parts.append(f"Рисунок {self.figure_no}:\n{text}")

    def table(self, table, caption: str = "") -> None:
        grid, nested = _table_grid(table)
        md_table = make_table(grid, caption=caption)
        if md_table is None:
            return
        own_caption = bool(self.parts) and self.parts[-1].lstrip().startswith(CAPTION_PREFIX)
        self.table_no += 1
        self.parts.append(render_md(md_table, number=None if own_caption else self.table_no))
        for inner in nested:
            self.table(inner, caption=f"вложена в таблицу {self.table_no}")
        if caption == "":
            self.images(table._tbl)  # картинки ячеек (и вложенных таблиц) — после таблицы


def _walk(container, doc, em: _Emitter) -> None:
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in container.iterchildren():
        if child.tag == qn("w:p"):
            par = Paragraph(child, doc)
            text = " ".join(par.text.split())
            if text:
                is_list = _is_list_paragraph(par)
                level = _paragraph_heading_level(par) or _numbered_heading_level(text, is_list)
                if level:
                    em.parts.append("#" * level + " " + text)
                elif is_list:
                    em.parts.append("- " + text)
                else:
                    em.parts.append(text)
            em.parts += _textbox_texts(child)
            em.images(child)
        elif child.tag == qn("w:tbl"):
            em.table(Table(child, doc))
        elif child.tag == qn("w:sdt"):
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                _walk(content, doc, em)


def _header_footer_lines(doc) -> list[str]:
    seen: list[str] = []
    for section in doc.sections:
        for part in (section.header, section.first_page_header, section.even_page_header,
                     section.footer, section.first_page_footer, section.even_page_footer):
            try:
                if part.is_linked_to_previous:
                    continue
                texts = [" ".join(p.text.split()) for p in part.paragraphs]
                for tbl in part.tables:
                    for row in tbl.rows:
                        texts.append(" ".join(" ".join(c.text.split()) for c in _row_cells(row)))
            except Exception:
                continue
            for t in texts:
                if t and t not in seen:
                    seen.append(t)
    return seen


def extract_docx(path: Path, settings=None) -> str:
    """``settings`` живого прогона включает чтение картинок vision-моделью; без него — только текст и таблицы."""
    if path.suffix.lower() in LEGACY_WORD_SUFFIXES:
        from app.app.services.office_convert import convert_with_soffice

        with tempfile.TemporaryDirectory() as tmp:
            converted = convert_with_soffice(path, Path(tmp), "docx")
            return extract_docx(converted, settings)

    from docx import Document

    doc = Document(str(path))
    em = _Emitter(doc, settings)
    _walk(doc.element.body, doc, em)
    hf = _header_footer_lines(doc)
    if hf:
        em.parts.append("## Колонтитулы\n" + "\n".join(hf))
    logger.debug("docx %s: blocks=%s tables=%s figures=%s", path.name, len(em.parts), em.table_no, em.figure_no)
    return "\n\n".join(em.parts).strip()
