"""Конвертация старых офисных форматов через LibreOffice headless: .doc/.rtf/.odt → .docx, .xls/.ods → .xlsx, .ppt → .pptx."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def find_soffice() -> str | None:
    env = os.environ.get("SOFFICE_PATH")
    if env and Path(env).exists():
        return env
    bundled = Path(__file__).resolve().parents[3] / "tools" / "libreoffice" / "program" / "soffice.exe"
    if bundled.is_file():  # копия внутри проекта — переносится вместе с папкой
        return str(bundled)
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base and (Path(base) / "LibreOffice" / "program" / "soffice.exe").exists():
            return str(Path(base) / "LibreOffice" / "program" / "soffice.exe")
    return None


def convert_with_soffice(src: Path, out_dir: Path, target: str, *, timeout: int = 180) -> Path:
    """``src`` → ``out_dir/<stem>.<target>``; RuntimeError, если LibreOffice нет или файл не получился."""
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError(
            f"для {src.suffix} нужен LibreOffice (soffice); задайте SOFFICE_PATH или пересохраните файл как .{target}"
        )
    profile = out_dir / "lo_profile"  # отдельный профиль: не конфликтуем с запущенным LibreOffice
    cmd = [soffice, f"-env:UserInstallation={profile.resolve().as_uri()}", "--headless", "--convert-to", target,
           "--outdir", str(out_dir), str(src)]
    subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    result = out_dir / (src.stem + "." + target)
    if not result.exists():
        raise RuntimeError(f"не удалось конвертировать {src.suffix} в .{target}")
    return result
