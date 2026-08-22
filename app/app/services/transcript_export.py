from __future__ import annotations

import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_LINE_SPACING
from docx.shared import Pt, RGBColor

from app.app.config import ROOT, Settings
from app.app.models.artifact import Artifact
from app.app.services.plain_text import clean_summary_output
from app.app.services.storage import artifact_dir, ensure_artifact_workspace, parsed_dir

_ASR = ROOT / "asr"
if str(_ASR) not in sys.path:
    sys.path.insert(0, str(_ASR))

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


def _set_default_style(doc: Document) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    normal.paragraph_format.line_spacing = 1.15
    normal.paragraph_format.space_after = Pt(6)


def _add_inline_runs(paragraph, text: str) -> None:
    parts = re.split(r"\*\*(.+?)\*\*", text)
    for i, part in enumerate(parts):
        if not part:
            continue
        run = paragraph.add_run(part)
        run.font.name = "Calibri"
        run.font.size = Pt(11)
        if i % 2 == 1:
            run.bold = True


def _markdown_to_docx(doc: Document, summary_md: str) -> None:
    for raw_line in summary_md.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("### "):
            doc.add_heading(line[4:].strip(), level=3)
        elif line.startswith("## "):
            doc.add_heading(line[3:].strip(), level=2)
        elif line.startswith("# "):
            doc.add_heading(line[2:].strip(), level=1)
        elif line.startswith("- ") or line.startswith("* "):
            p = doc.add_paragraph(style="List Bullet")
            _add_inline_runs(p, line[2:].strip())
        else:
            p = doc.add_paragraph()
            _add_inline_runs(p, line)


def build_summary_docx_bytes(summary_md: str, artifact: Artifact) -> bytes:
    """Word .docx через python-docx (тот же стек, что для входных документов)."""
    doc = Document()
    _set_default_style(doc)

    title = Path(artifact.original_filename).stem or artifact.artifact_id
    h = doc.add_heading("Итоговая выжимка созвона", level=1)
    h.runs[0].font.color.rgb = RGBColor(0x1E, 0x29, 0x3B)

    meta = doc.add_paragraph()
    run = meta.add_run(artifact.original_filename)
    run.italic = True
    run.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)
    run.font.size = Pt(10)

    stamp = doc.add_paragraph()
    srun = stamp.add_run(f"Экспорт: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    srun.font.size = Pt(9)
    srun.font.color.rgb = RGBColor(0x94, 0xA3, 0xB8)

    doc.add_paragraph()
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


def resolve_summary_docx(settings: Settings, artifact: Artifact) -> tuple[bytes, str] | None:
    summary_md = ""
    for path in _summary_md_candidates(settings, artifact):
        if path.is_file():
            summary_md = path.read_text(encoding="utf-8").strip()
            if summary_md:
                break
    if not summary_md:
        return None

    title = Path(artifact.original_filename).stem or artifact.artifact_id
    filename = f"{title}_выжимка.docx"
    return build_summary_docx_bytes(summary_md, artifact), filename


def transcript_export_available(settings: Settings, artifact: Artifact) -> bool:
    return resolve_transcript_txt(settings, artifact) is not None


def summary_export_available(settings: Settings, artifact: Artifact) -> bool:
    return resolve_summary_docx(settings, artifact) is not None
