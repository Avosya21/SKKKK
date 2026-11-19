"""Remux локальной записи созвона (WebM/MP4 от MediaRecorder) в seekable MP4 через ffmpeg."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool

# Локальная запись 1080p может быть большой; лимит защищает процесс, не ASR.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
REMUX_TIMEOUT_SEC = 300


class RemuxError(Exception):
    """Не удалось пересобрать файл в seekable MP4."""


def remux_to_seekable_mp4(src_bytes: bytes, *, work_dir: Path | None = None) -> bytes:
    """Пересобрать в H.264+AAC MP4 с +faststart (перемотка и звук во всех плеерах).

    Copy запрещён: MediaRecorder даёт VP8/VP9+Opus — в «MP4» через copy Windows
    показывает видео без звука («формат Opus не поддерживается»).
    """
    if not src_bytes:
        raise RemuxError("пустой файл")
    if len(src_bytes) > MAX_UPLOAD_BYTES:
        raise RemuxError("файл слишком большой")

    ffmpeg = resolve_ffmpeg_tool("ffmpeg")
    base = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="local-record-remux-"))
    base.mkdir(parents=True, exist_ok=True)
    src = base / "in.bin"
    dst = base / "out.mp4"
    src.write_bytes(src_bytes)

    try:
        ok = _run_ffmpeg(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(src),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                str(dst),
            ]
        )
        if not ok:
            raise RemuxError("ffmpeg не смог пересобрать запись в H.264/AAC MP4")
        out = dst.read_bytes()
        if len(out) < 32 or b"ftyp" not in out[:64]:
            raise RemuxError("пустой или не-MP4 результат remux")
        return out
    finally:
        for path in (src, dst):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if work_dir is None:
            try:
                base.rmdir()
            except OSError:
                pass


def _run_ffmpeg(cmd: list[str]) -> bool:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=REMUX_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and Path(cmd[-1]).is_file() and Path(cmd[-1]).stat().st_size > 0
