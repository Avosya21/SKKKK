from __future__ import annotations

import os
import shutil
from pathlib import Path

_WIN_CANDIDATES = (
    Path(r"C:\ffmpeg-8.0-full_build\bin"),
    Path(r"C:\ffmpeg\bin"),
)


def resolve_ffmpeg_tool(name: str) -> str:
    """Return path to ffmpeg or ffprobe executable."""
    if name not in {"ffmpeg", "ffprobe"}:
        raise ValueError(f"unsupported tool: {name}")

    env_dir = os.environ.get("FFMPEG_DIR")
    if env_dir:
        candidate = Path(env_dir) / f"{name}.exe"
        if candidate.is_file():
            return str(candidate)

    env_bin = os.environ.get(f"{name.upper()}_BIN") or os.environ.get("FFMPEG_BIN")
    if env_bin:
        candidate = Path(env_bin)
        if candidate.is_file():
            return str(candidate)

    found = shutil.which(name)
    if found:
        return found

    for base in _WIN_CANDIDATES:
        candidate = base / f"{name}.exe"
        if candidate.is_file():
            return str(candidate)

    raise FileNotFoundError(
        f"{name} not found in PATH; set FFMPEG_DIR or install to C:\\ffmpeg-8.0-full_build\\bin"
    )
