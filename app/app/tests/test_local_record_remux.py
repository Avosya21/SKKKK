"""Remux локальной записи: seekable MP4 через ffmpeg."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool
from app.app.services.local_record_remux import RemuxError, remux_to_seekable_mp4


def _ffmpeg() -> str:
    return resolve_ffmpeg_tool("ffmpeg")


def _tiny_webm(tmp_path: Path) -> bytes:
    """Минимальный WebM (цвет + тишина) — как сырой MediaRecorder без индексов."""
    out = tmp_path / "tiny.webm"
    cmd = [
        _ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=320x240:d=1",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=mono",
        "-t",
        "1",
        "-c:v",
        "libvpx",
        "-c:a",
        "libopus",
        "-shortest",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 32:
        pytest.skip("ffmpeg не собрал тестовый WebM")
    return out.read_bytes()


def test_remux_to_seekable_mp4_has_moov(tmp_path: Path):
    raw = _tiny_webm(tmp_path)
    out = remux_to_seekable_mp4(raw, work_dir=tmp_path)
    assert out[:4] == b"\x00\x00\x00\x18" or b"ftyp" in out[:64]
    assert b"moov" in out  # faststart: moov не только в хвосте
    # ffprobe: длительность + H.264/AAC (не VP8/Opus — иначе Windows без звука)
    probe = resolve_ffmpeg_tool("ffprobe")
    mp4 = tmp_path / "check.mp4"
    mp4.write_bytes(out)
    proc = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(mp4),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0
    assert float(proc.stdout.strip()) > 0.2
    codecs = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(mp4),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert codecs.returncode == 0
    names = {line.strip() for line in codecs.stdout.splitlines() if line.strip()}
    assert "h264" in names
    assert "aac" in names
    assert "opus" not in names
    assert "vp8" not in names and "vp9" not in names


def test_remux_rejects_empty():
    with pytest.raises(RemuxError):
        remux_to_seekable_mp4(b"")


def test_remux_api_requires_auth(client: TestClient):
    res = client.post("/api/meetings/local-record/remux", files={"file": ("x.webm", b"abc", "video/webm")})
    assert res.status_code in (401, 403)


def test_remux_api_ok(client: TestClient, manager_token: str, tmp_path: Path):
    raw = _tiny_webm(tmp_path)
    res = client.post(
        "/api/meetings/local-record/remux",
        headers={"Authorization": f"Bearer {manager_token}"},
        files={"file": ("sozvon.webm", raw, "video/webm")},
    )
    assert res.status_code == 200, res.text
    assert res.headers.get("content-type", "").startswith("video/mp4")
    assert b"moov" in res.content
