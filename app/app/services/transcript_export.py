from __future__ import annotations

import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from app.app.config import ROOT, Settings
from app.app.models.artifact import Artifact
from app.app.services.plain_text import clean_summary_output
from app.app.services.storage import artifact_dir, ensure_artifact_workspace, parsed_dir

_ASR = ROOT / "asr"
if str(_ASR) not in sys.path:
    sys.path.insert(0, str(_ASR))

_FONT = "Calibri"
_ACCENT = "5B7FA6"
_INK = RGBColor(0x1E, 0x29, 0x3B)
_MUTED = RGBColor(0x64, 0x74, 0x8B)
_FAINT = RGBColor(0x94, 0xA3, 0xB8)
_HEAD3 = RGBColor(0x33, 0x41, 0x55)
_INLINE_TOKEN = re.compile(r"(\*\*[^*]+?\*\*|\*[^*]+?\*|`[^`]+?`)")
_NUMBERED = re.compile(r"^(\d+)[.)]\s+(.*)$")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")

from speakers import Turn, format_turns_timecodes  # noqa: E402


def _speakers_json_candidates(settings: Settings, artifact: Artifact) -> list[Path]:
    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    parsed = parsed_dir(settings, artifact.project_id, artifact.source_type)
    base = artifact.artifact_id
    return [
        ws / "asr" / "docker_gigaam_speakers.json",
        ws / "asr" / "docker_merged_speakers.json",
        parsed / f"{base}_speakers.json",
    ]


def _summary_md_candidates(settings: Settings, artifact: Artifact) -> list[Path]:
    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    parsed = parsed_dir(settings, artifact.project_id, artifact.source_type)
    base = artifact.artifact_id
    return [
        ws / "asr" / "call_summary.md",
        ws / "reports" / "call_summary.md",
        parsed / f"{base}_call_summary.md",
    ]


def _brief_md_candidates(settings: Settings, artifact: Artifact) -> list[Path]:
    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    parsed = parsed_dir(settings, artifact.project_id, artifact.source_type)
    base = artifact.artifact_id
    return [
        ws / "asr" / "call_brief.md",
        ws / "reports" / "call_brief.md",
        parsed / f"{base}_call_brief.md",
    ]


def _timecodes_txt_candidates(settings: Settings, artifact: Artifact) -> list[Path]:
    ws = ensure_artifact_workspace(settings, artifact.project_id, artifact.artifact_id)
    parsed = parsed_dir(settings, artifact.project_id, artifact.source_type)
    base = artifact.artifact_id
    return [
        ws / "asr" / "docker_merged_timecodes.txt",
        ws / "asr" / "docker_gigaam_diar_smooth_timecodes.txt",
        parsed / f"{base}_final.txt",
    ]


def _load_turns(path: Path) -> list[Turn]:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("turns") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    turns: list[Turn] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        turns.append(
            Turn(
                start=float(row.get("start") or 0),
                end=float(row.get("end") or 0),
                speaker=str(row.get("speaker") or "Спикер"),
                text=text,
            )
        )
    return turns


def _set_rfonts(run, name: str = _FONT) -> None:
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rFonts.set(qn(attr), name)


def _apply_font(
    run,
    *,
    size_pt: float,
    bold: bool | None = None,
    italic: bool | None = None,
    color: RGBColor | None = None,
    name: str = _FONT,
) -> None:
    run.font.name = name
    run.font.size = Pt(size_pt)
    _set_rfonts(run, name)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if color is not None:
        run.font.color.rgb = color


def _set_style_font(style, *, size_pt: float, bold: bool | None = None) -> None:
    font = style.font
    font.name = _FONT
    font.size = Pt(size_pt)
    if bold is not None:
        font.bold = bold
    rPr = style.element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rFonts.set(qn(attr), _FONT)


def _set_default_style(doc: Document) -> None:
    normal = doc.styles["Normal"]
    _set_style_font(normal, size_pt=11)
    pf = normal.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = 1.15
    pf.space_after = Pt(6)
    for section in doc.sections:
        section.top_margin = Cm(1.8)
        section.bottom_margin = Cm(1.8)
        section.left_margin = Cm(2.2)
        section.right_margin = Cm(2.2)
        section.page_width = Cm(21.0)
        section.page_height = Cm(29.7)


def _add_text_run(
    paragraph,
    text: str,
    *,
    size_pt: float = 11,
    bold: bool = False,
    italic: bool = False,
    name: str = _FONT,
    color: RGBColor | None = None,
) -> None:
    if not text:
        return
    run = paragraph.add_run(text)
    _apply_font(run, size_pt=size_pt, bold=bold, italic=italic, color=color, name=name)


def _add_inline_runs(paragraph, text: str, *, size_pt: float = 11) -> None:
    text = _LINK.sub(r"\1", text)
    pos = 0
    for match in _INLINE_TOKEN.finditer(text):
        if match.start() > pos:
            _add_text_run(paragraph, text[pos : match.start()], size_pt=size_pt)
        chunk = match.group(0)
        if chunk.startswith("**"):
            _add_text_run(paragraph, chunk[2:-2], size_pt=size_pt, bold=True)
        elif chunk.startswith("`"):
            _add_text_run(paragraph, chunk[1:-1], size_pt=size_pt, name="Consolas")
        else:
            _add_text_run(paragraph, chunk[1:-1], size_pt=size_pt, italic=True)
        pos = match.end()
    if pos < len(text):
        _add_text_run(paragraph, text[pos:], size_pt=size_pt)


def _set_outline_level(paragraph, level: int) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    for old in pPr.findall(qn("w:outlineLvl")):
        pPr.remove(old)
    outline = OxmlElement("w:outlineLvl")
    outline.set(qn("w:val"), str(level))
    pPr.append(outline)


def _set_bottom_border(paragraph, color: str = "CBD5E1") -> None:
    pPr = paragraph._p.get_or_add_pPr()
    for old in pPr.findall(qn("w:pBdr")):
        pPr.remove(old)
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "12")
    bottom.set(qn("w:space"), "8")
    bottom.set(qn("w:color"), color)
    pBdr.append(bottom)
    pPr.append(pBdr)


def _add_heading_para(doc: Document, text: str, level: int) -> None:
    sizes = {1: 18, 2: 14, 3: 12}
    colors = {1: _INK, 2: _INK, 3: _HEAD3}
    before = {1: 0, 2: 16, 3: 10}
    after = {1: 10, 2: 6, 3: 4}
    p = doc.add_paragraph()
    run = p.add_run(text)
    _apply_font(run, size_pt=sizes[level], bold=True, color=colors[level])
    pf = p.paragraph_format
    pf.space_before = Pt(before[level])
    pf.space_after = Pt(after[level])
    pf.keep_with_next = True
    pf.line_spacing = 1.15
    _set_outline_level(p, level - 1)
    if level == 1:
        _set_bottom_border(p)


def _add_body_para(doc: Document, text: str, *, left_cm: float = 0, bullet: str = "") -> None:
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(4)
    pf.line_spacing = 1.15
    if left_cm:
        pf.left_indent = Cm(left_cm)
        pf.first_line_indent = Cm(-0.35)
    if bullet:
        _add_text_run(p, bullet, size_pt=11, color=_MUTED)
    _add_inline_runs(p, text, size_pt=11)


def _shade_cell(cell, fill: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    for old in tcPr.findall(qn("w:shd")):
        tcPr.remove(old)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcPr.append(shd)


def _set_cell_margins(cell, *, top: int = 60, bottom: int = 60, left: int = 80, right: int = 80) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    for old in tcPr.findall(qn("w:tcMar")):
        tcPr.remove(old)
    tcMar = OxmlElement("w:tcMar")
    for edge, value in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        node = OxmlElement(f"w:{edge}")
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")
        tcMar.append(node)
    tcPr.append(tcMar)


def _set_table_full_width(table) -> None:
    tbl = table._tbl
    tblPr = tbl.tblPr
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl.insert(0, tblPr)
    for old in tblPr.findall(qn("w:tblW")):
        tblPr.remove(old)
    tblW = OxmlElement("w:tblW")
    tblW.set(qn("w:w"), "5000")
    tblW.set(qn("w:type"), "pct")
    tblPr.append(tblW)


def _set_col_widths(table, percents: list[int]) -> None:
    """Column widths as percents of usable page (sum ≈ 100)."""
    grid = table._tbl.tblGrid
    if grid is None:
        return
    usable_dxa = 9072
    for idx, grid_col in enumerate(grid.findall(qn("w:gridCol"))):
        if idx < len(percents):
            grid_col.set(qn("w:w"), str(int(usable_dxa * percents[idx] / 100)))
    for row in table.rows:
        for idx, cell in enumerate(row.cells):
            if idx >= len(percents):
                continue
            tc = cell._tc
            tcPr = tc.get_or_add_tcPr()
            for old in tcPr.findall(qn("w:tcW")):
                tcPr.remove(old)
            tcW = OxmlElement("w:tcW")
            tcW.set(qn("w:w"), str(int(5000 * percents[idx] / 100)))
            tcW.set(qn("w:type"), "pct")
            tcPr.append(tcW)


def _style_summary_table(table, ncols: int) -> None:
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    _set_table_full_width(table)
    if ncols == 3:
        _set_col_widths(table, [22, 56, 22])
    elif ncols == 2:
        _set_col_widths(table, [32, 68])
    header = table.rows[0]
    for cell in header.cells:
        _shade_cell(cell, _ACCENT)
        _set_cell_margins(cell)
        for paragraph in cell.paragraphs:
            paragraph.paragraph_format.space_before = Pt(0)
            paragraph.paragraph_format.space_after = Pt(0)
            for run in paragraph.runs:
                _apply_font(run, size_pt=10, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF))
    for r_i, row in enumerate(table.rows[1:], start=1):
        fill = "F8FAFC" if r_i % 2 == 0 else "FFFFFF"
        for cell in row.cells:
            _shade_cell(cell, fill)
            _set_cell_margins(cell)
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_before = Pt(0)
                paragraph.paragraph_format.space_after = Pt(0)
                for run in paragraph.runs:
                    _apply_font(run, size_pt=10, color=_INK)


def _markdown_to_docx(doc: Document, summary_md: str) -> None:
    from transcript_view import _looks_md_table_row, parse_md_table

    lines = summary_md.strip().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or re.fullmatch(r"-{3,}", line):
            i += 1
            continue
        if _looks_md_table_row(line):
            table_lines = [line]
            i += 1
            while i < len(lines) and _looks_md_table_row(lines[i].strip()):
                table_lines.append(lines[i].strip())
                i += 1
            parsed = parse_md_table(table_lines)
            if parsed is None:
                for row in table_lines:
                    _add_body_para(doc, row)
                continue
            header, body = parsed
            table = doc.add_table(rows=1 + len(body), cols=len(header))
            for col, cell in enumerate(header):
                _add_inline_runs(table.rows[0].cells[col].paragraphs[0], cell, size_pt=10)
            for r_i, row in enumerate(body, start=1):
                for col, cell in enumerate(row):
                    _add_inline_runs(table.rows[r_i].cells[col].paragraphs[0], cell, size_pt=10)
            _style_summary_table(table, len(header))
            spacer = doc.add_paragraph()
            spacer.paragraph_format.space_after = Pt(8)
            spacer.paragraph_format.space_before = Pt(2)
            continue
        if line.startswith("#### "):
            _add_heading_para(doc, line[5:].strip(), 3)
        elif line.startswith("### "):
            _add_heading_para(doc, line[4:].strip(), 3)
        elif line.startswith("## "):
            _add_heading_para(doc, line[3:].strip(), 2)
        elif line.startswith("# "):
            _add_heading_para(doc, line[2:].strip(), 2)
        elif line.startswith("- ") or line.startswith("* "):
            _add_body_para(doc, line[2:].strip(), left_cm=0.75, bullet="• ")
        else:
            numbered = _NUMBERED.match(line)
            if numbered:
                _add_body_para(
                    doc,
                    numbered.group(2).strip(),
                    left_cm=0.75,
                    bullet=f"{numbered.group(1)}. ",
                )
            else:
                _add_body_para(doc, line)
        i += 1


def build_summary_docx_bytes(
    summary_md: str,
    artifact: Artifact,
    *,
    heading: str = "Итоговая выжимка созвона",
) -> bytes:
    """Word .docx через python-docx (тот же стек, что для входных документов)."""
    doc = Document()
    _set_default_style(doc)
    _add_heading_para(doc, heading, 1)

    meta = doc.add_paragraph()
    meta.paragraph_format.space_before = Pt(0)
    meta.paragraph_format.space_after = Pt(2)
    _add_text_run(meta, artifact.original_filename, size_pt=10, italic=True, color=_MUTED)

    stamp = doc.add_paragraph()
    stamp.paragraph_format.space_before = Pt(0)
    stamp.paragraph_format.space_after = Pt(12)
    _add_text_run(
        stamp,
        f"Экспорт: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        size_pt=9,
        color=_FAINT,
    )

    _markdown_to_docx(doc, clean_summary_output(summary_md))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def resolve_transcript_txt(settings: Settings, artifact: Artifact) -> tuple[str, str] | None:
    """Текст стенограммы и имя файла для скачивания."""
    for path in _speakers_json_candidates(settings, artifact):
        if path.is_file():
            turns = _load_turns(path)
            if turns:
                body = format_turns_timecodes(turns)
                name = f"{Path(artifact.original_filename).stem}_стенограмма.txt"
                return body, name

    for path in _timecodes_txt_candidates(settings, artifact):
        if path.is_file() and path.stat().st_size > 0:
            body = path.read_text(encoding="utf-8")
            if body.strip():
                name = f"{Path(artifact.original_filename).stem}_стенограмма.txt"
                return body if body.endswith("\n") else body + "\n", name
    return None


def _first_nonempty_md(paths: list[Path]) -> str:
    for path in paths:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
    return ""


def resolve_summary_docx(settings: Settings, artifact: Artifact) -> tuple[bytes, str] | None:
    summary_md = _first_nonempty_md(_summary_md_candidates(settings, artifact))
    if not summary_md:
        return None

    title = Path(artifact.original_filename).stem or artifact.artifact_id
    filename = f"{title}_выжимка.docx"
    return build_summary_docx_bytes(summary_md, artifact), filename


def resolve_brief_docx(settings: Settings, artifact: Artifact) -> tuple[bytes, str] | None:
    brief_md = _first_nonempty_md(_brief_md_candidates(settings, artifact))
    if not brief_md:
        return None

    title = Path(artifact.original_filename).stem or artifact.artifact_id
    filename = f"{title}_краткое_резюме.docx"
    return (
        build_summary_docx_bytes(brief_md, artifact, heading="Краткое резюме созвона"),
        filename,
    )


def transcript_export_available(settings: Settings, artifact: Artifact) -> bool:
    return resolve_transcript_txt(settings, artifact) is not None


def summary_export_available(settings: Settings, artifact: Artifact) -> bool:
    return resolve_summary_docx(settings, artifact) is not None


def brief_export_available(settings: Settings, artifact: Artifact) -> bool:
    return resolve_brief_docx(settings, artifact) is not None
