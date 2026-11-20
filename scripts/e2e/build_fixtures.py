"""Собрать синтетические фикстуры e2e (без персональных данных)."""
from __future__ import annotations

import wave
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches, Pt

from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FIXTURES = HERE / "fixtures"
ANCHOR = "E2E_ACCEPTANCE_TOKEN_7F3A"
# Уникальный штамп — иначе upload дедупит sha256 и вернёт старый done-артефакт.
RUN_STAMP = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
BODY = (
    f"Протокол приёмки PrimaryAnalitics на новой машине.\n"
    f"Якорный код: {ANCHOR}.\n"
    f"Прогон: {RUN_STAMP}.\n"
    f"Решили: проверить загрузку документов всех форматов и живой конвейер ASR.\n"
    f"Срок: пятница. Ответственный: менеджер песочницы.\n"
)


def _write_txt() -> Path:
    p = FIXTURES / "e2e_note.txt"
    p.write_text(BODY, encoding="utf-8")
    return p


def _write_html() -> Path:
    p = FIXTURES / "e2e_page.html"
    p.write_text(
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>E2E</title></head>"
        f"<body><h1>Приёмка</h1><p>{BODY.replace(chr(10), '<br>')}</p></body></html>",
        encoding="utf-8",
    )
    return p


def _write_json() -> Path:
    p = FIXTURES / "e2e_export.json"
    p.write_text(
        '{"title":"e2e","anchor":"%s","run":"%s","decisions":["проверить ASR","проверить документы"]}\n'
        % (ANCHOR, RUN_STAMP),
        encoding="utf-8",
    )
    return p


def _write_docx() -> Path:
    p = FIXTURES / "e2e_protocol.docx"
    doc = Document()
    doc.add_heading("Протокол e2e", level=1)
    for line in BODY.splitlines():
        doc.add_paragraph(line)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Якорь"
    table.cell(0, 1).text = ANCHOR
    table.cell(1, 0).text = "Статус"
    table.cell(1, 1).text = "приёмка"
    doc.save(p)
    return p


def _write_xlsx() -> Path:
    p = FIXTURES / "e2e_estimate.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Смета"
    ws.append(["Позиция", "Сумма", "Комментарий"])
    ws.append(["Приёмка портала", 1, ANCHOR])
    ws.append(["ASR e2e", 2, f"живой конвейер {RUN_STAMP}"])
    wb.save(p)
    return p


def _write_pptx() -> Path:
    p = FIXTURES / "e2e_slides.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "E2E приёмка"
    body = slide.placeholders[1]
    body.text = BODY
    # ensure size ok
    _ = Inches(1), Pt(14)
    prs.save(p)
    return p


def _write_pdf() -> Path:
    p = FIXTURES / "e2e_doc.pdf"
    # Минимальный PDF с текстом якоря + stamp (Helvetica, латиница).
    stream = f"BT /F1 12 Tf 40 120 Td ({ANCHOR} {RUN_STAMP}) Tj ET".encode("latin-1", errors="replace")
    objects = []
    objects.append(b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n")
    objects.append(b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n")
    objects.append(
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] "
        b"/Contents 4 0 R /Resources<< /Font<< /F1 5 0 R >> >> >>endobj\n"
    )
    objects.append(f"4 0 obj<< /Length {len(stream)} >>stream\n".encode() + stream + b"\nendstream\nendobj\n")
    objects.append(b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n")
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)
    xref_at = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode())
    out.extend(f"trailer<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode())
    p.write_bytes(bytes(out))
    return p


def _write_csv() -> Path:
    p = FIXTURES / "e2e_table.csv"
    p.write_text(
        f"позиция;сумма;комментарий\nприёмка;1;{ANCHOR}\nasr;2;прогон {RUN_STAMP}\n",
        encoding="utf-8",
    )
    return p


def _write_odt() -> Path:
    """Минимальный ODT (zip) с якорем — ветка LibreOffice Word."""
    import zipfile

    p = FIXTURES / "e2e_protocol.odt"
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 office:version="1.2">
 <office:body><office:text>
  <text:p>Протокол e2e ODT. Якорь: {ANCHOR}. Прогон: {RUN_STAMP}.</text:p>
 </office:text></office:body>
</office:document-content>
"""
    manifest = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">
 <manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>
 <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
</manifest:manifest>
"""
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        zf.writestr("content.xml", content)
        zf.writestr("META-INF/manifest.xml", manifest)
    return p


def _write_png() -> Path:
    """Минимальный PNG 8×8 с текстом якоря нельзя впихнуть в пиксели — OCR-фикстура:
    пишем белый PNG и рядом подпись в metadata; для OCR якорь рисуем через подпись в имени
    и отдельный txt рядом не годится. Используем Pillow если есть, иначе крошечный валидный PNG
    + тот же якорь только в имени файла не пройдёт проверку текста — поэтому предпочтительно Pillow.
    """
    p = FIXTURES / "e2e_scan.png"
    try:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.new("RGB", (640, 120), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.text((10, 40), f"{ANCHOR} {RUN_STAMP}", fill=(0, 0, 0))
        img.save(p)
        return p
    except Exception:
        # 1×1 PNG; без OCR-текста — e2e_documents для png проверит done, якорь опционален
        p.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
            b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return p


def _write_mp4() -> Path | None:
    """Короткий mp4 из e2e_speech.wav (если есть ffmpeg) — ветка видео-созвона."""
    wav = FIXTURES / "e2e_speech.wav"
    p = FIXTURES / "e2e_speech.mp4"
    if not wav.is_file():
        return None
    import shutil
    import subprocess

    ff = shutil.which("ffmpeg")
    if not ff:
        tools = ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
        ff = str(tools) if tools.is_file() else None
    if not ff:
        return None
    r = subprocess.run(
        [ff, "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "64k", "-shortest", str(p)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if r.returncode != 0 or not p.is_file() or p.stat().st_size < 1000:
        return None
    return p


def _concat_wav(sources: list[Path], dest: Path) -> Path:
    frames = []
    params = None
    for src in sources:
        with wave.open(str(src), "rb") as w:
            if params is None:
                params = w.getparams()
            elif (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (
                params.nchannels,
                params.sampwidth,
                params.framerate,
            ):
                raise RuntimeError(f"несовместимый WAV: {src}")
            frames.append(w.readframes(w.getnframes()))
    assert params is not None
    with wave.open(str(dest), "wb") as out:
        out.setparams(params)
        for chunk in frames:
            out.writeframes(chunk)
    return dest


def _write_speech() -> Path:
    a = ROOT / "scripts" / "qa_data_speech_a.wav"
    b = ROOT / "scripts" / "qa_data_speech_b.wav"
    dest = FIXTURES / "e2e_speech.wav"
    if not a.is_file():
        raise FileNotFoundError(f"нет {a}")
    parts = [a]
    if b.is_file():
        parts.append(b)
    _concat_wav(parts, dest)
    # Дописываем уникальный хвост тишины разной длины — другой sha256 при том же звуке.
    import wave

    with wave.open(str(dest), "rb") as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    # ~0.01–0.05 с тишины, зависит от stamp
    n = 160 + (int(RUN_STAMP[-2:]) % 40) * 80
    silence = b"\x00\x00" * n
    with wave.open(str(dest), "wb") as out:
        out.setparams(params)
        out.writeframes(frames + silence)
    return dest


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    built: list[Path] = [
        _write_txt(),
        _write_html(),
        _write_json(),
        _write_docx(),
        _write_xlsx(),
        _write_pptx(),
        _write_pdf(),
        _write_csv(),
        _write_odt(),
        _write_png(),
        _write_speech(),
    ]
    mp4 = _write_mp4()
    if mp4 is not None:
        built.append(mp4)
    for p in built:
        print(f"OK {p.relative_to(ROOT)} ({p.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
