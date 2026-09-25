"""Поиск Tesseract и каталога tessdata, настройка pytesseract."""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

from app.app.config import ROOT

_WIN_EXE_CANDIDATES = (
    Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
)

_PROJECT_TESSDATA = ROOT / "tools" / "tesseract" / "tessdata"


def resolve_tesseract_cmd() -> str:
    env_bin = os.environ.get("TESSERACT_CMD") or os.environ.get("TESSERACT_BIN")
    if env_bin:
        candidate = Path(env_bin)
        if candidate.is_file():
            return str(candidate)

    bundled = ROOT / "tools" / "tesseract" / "tesseract.exe"  # портативная копия внутри проекта
    if bundled.is_file():
        return str(bundled)

    found = shutil.which("tesseract")
    if found:
        return found

    for candidate in _WIN_EXE_CANDIDATES:
        if candidate.is_file():
            return str(candidate)

    raise FileNotFoundError(
        "tesseract not found; install UB-Mannheim Tesseract-OCR or set TESSERACT_CMD"
    )


def resolve_tessdata_dir() -> Path | None:
    env_dir = os.environ.get("TESSDATA_DIR")
    if env_dir:
        path = Path(env_dir)
        if path.is_dir():
            return path

    if (_PROJECT_TESSDATA / "rus.traineddata").is_file() and (
        _PROJECT_TESSDATA / "eng.traineddata"
    ).is_file():
        return _PROJECT_TESSDATA

    for base in (
        Path(r"C:\Program Files\Tesseract-OCR\tessdata"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tessdata"),
    ):
        if (base / "rus.traineddata").is_file() and (base / "eng.traineddata").is_file():
            return base
    return None


@lru_cache
def configure_pytesseract() -> str:
    """Point pytesseract at the binary; return optional ``--tessdata-dir`` config."""
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = resolve_tesseract_cmd()
    tessdata = resolve_tessdata_dir()
    if tessdata is None:
        return ""
    # Tesseract 5 expects TESSDATA_PREFIX to be the tessdata directory itself.
    # Do not quote the path in config: pytesseract splits on spaces and keeps quotes.
    os.environ["TESSDATA_PREFIX"] = str(tessdata)
    return f"--tessdata-dir {tessdata}"
