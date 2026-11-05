"""Текст из презентаций .pptx → markdown по слайдам (вход для normalize / knowledge-пайплайна).

Каждый слайд — секция `## Слайд N: заголовок`, чтобы normalize_document резал текст по границам слайдов,
а индексация/цитаты могли ссылаться на номер слайда. Извлекаем: заголовок, текст по порядку чтения,
таблицы (markdown), группы фигур, диаграммы (данные рядов), заметки докладчика, текст картинок всех слайдов
(vision-модель с подсказкой OCR; одна и та же картинка читается один раз, иконки меньше 200 px пропускаются).
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Iterator

from app.app.config import Settings
from app.app.services.office_convert import convert_with_soffice
from app.app.services.office_convert import find_soffice as _find_soffice  # noqa: F401 — старое имя для тестов

logger = logging.getLogger("primaryanalitics.pptx")

MIN_IMAGE_SIDE_PX = 200  # иконки/логотипы не распознаём


def _cell_text(cell) -> str:
    return " ".join(cell.text_frame.text.split()).replace("|", "\\|")


def _table_md(table) -> str:
    rows = [[_cell_text(c) for c in row.cells] for row in table.rows]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join("---" for _ in rows[0]) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _chart_md(chart) -> str:
    parts: list[str] = []
    try:
        if chart.has_title and chart.chart_title.has_text_frame:
            parts.append(f"Диаграмма: {chart.chart_title.text_frame.text.strip()}")
    except Exception:
        pass
    try:
        cats = [str(c) for c in chart.plots[0].categories]
        for plot in chart.plots:
            for series in plot.series:
                vals = list(series.values)
                pairs = "; ".join(f"{c}: {v:g}" if isinstance(v, float) else f"{c}: {v}" for c, v in zip(cats, vals))
                parts.append(f"- {series.name or 'ряд'}: {pairs}")
    except Exception as exc:
        logger.debug("chart data skipped: %s", exc)
    return "\n".join(parts)


def _paragraphs_md(text_frame) -> str:
    lines: list[str] = []
    for p in text_frame.paragraphs:
        text = "".join(r.text for r in p.runs).strip() or p.text.strip()
        if not text:
            continue
        lines.append(("  " * min(p.level, 4) + "- " + text) if p.level else text)
    return "\n".join(lines)


def _walk(shapes) -> Iterator:
    """Фигуры в порядке чтения (сверху вниз, слева направо), группы раскрываем."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    ordered = sorted(shapes, key=lambda s: ((s.top or 0) // 300000, s.left or 0))
    for shp in ordered:
        if shp.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _walk(shp.shapes)
        else:
            yield shp


def _smartart_text(shape) -> str:
    """SmartArt (graphicFrame без text_frame/table/chart): вытаскиваем a:t из связанных data-частей."""
    try:
        rid = shape._element.xpath(".//*[local-name()='relIds']/@*[local-name()='dm']")
        if not rid:
            return ""
        from lxml import etree

        part = shape.part.related_part(rid[0])
        root = etree.fromstring(part.blob)
        ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
        return "\n".join(t.strip() for t in root.xpath("//a:t/text()", namespaces=ns) if t.strip())
    except Exception:
        return ""


def _ocr_picture(shape, settings: Settings | None, seen: set[str] | None = None) -> str:
    from app.app.services.pipeline_documents import _extract_image

    try:
        img = shape.image
        if seen is not None:
            if img.sha1 in seen:
                return ""
            seen.add(img.sha1)
        if (img.size[0] or 0) < MIN_IMAGE_SIDE_PX or (img.size[1] or 0) < MIN_IMAGE_SIDE_PX:
            return ""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / f"img.{img.ext}"
            p.write_bytes(img.blob)
            return _extract_image(p, settings).strip()
    except Exception as exc:
        logger.warning("pptx picture OCR failed: %s", exc)
        return ""


def convert_ppt_to_pptx(src: Path, out_dir: Path, *, timeout: int = 180) -> Path:
    """Старый бинарный .ppt → .pptx через LibreOffice headless (python-pptx читает только OOXML)."""
    return convert_with_soffice(src, out_dir, "pptx", timeout=timeout)


def extract_pptx(path: Path, settings: Settings | None = None) -> str:
    if path.suffix.lower() == ".ppt":
        with tempfile.TemporaryDirectory() as tmp:
            converted = convert_ppt_to_pptx(path, Path(tmp))
            return extract_pptx(converted, settings)
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(str(path))
    sections: list[str] = []
    seen_images: set[str] = set()
    use_images = settings is not None and not settings.dry_run

    for num, slide in enumerate(prs.slides, start=1):
        title_shape = slide.shapes.title
        title = " ".join(title_shape.text_frame.text.split()) if title_shape is not None and title_shape.has_text_frame else ""
        hidden = slide._element.get("show") == "0"
        head = f"## Слайд {num}" + (f": {title}" if title else "") + (" (скрытый)" if hidden else "")

        blocks: list[str] = []
        pictures = []
        for shp in _walk(slide.shapes):
            if title_shape is not None and shp.shape_id == title_shape.shape_id:
                continue
            if shp.has_text_frame:
                md = _paragraphs_md(shp.text_frame)
                if md:
                    blocks.append(md)
            elif getattr(shp, "has_table", False) and shp.has_table:
                md = _table_md(shp.table)
                if md:
                    blocks.append(md)
            elif getattr(shp, "has_chart", False) and shp.has_chart:
                md = _chart_md(shp.chart)
                if md:
                    blocks.append(md)
            elif shp.shape_type == MSO_SHAPE_TYPE.PICTURE:
                pictures.append(shp)
            elif shp._element.tag.endswith("}graphicFrame"):
                md = _smartart_text(shp)
                if md:
                    blocks.append(md)

        if use_images:
            for pic in pictures:
                text = _ocr_picture(pic, settings, seen_images)
                if text:
                    blocks.append(f"Текст на изображении:\n{text}")

        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame
            note_text = notes.text.strip() if notes is not None else ""
            if note_text:
                blocks.append(f"Заметки докладчика:\n{note_text}")

        if blocks or title:
            sections.append(head + ("\n\n" + "\n\n".join(blocks) if blocks else ""))

    return "\n\n".join(sections).strip()
