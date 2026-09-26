"""Sortformer feed WAV normalization (gguf engine rejects LIST/INFO chunks)."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "asr"))

from dump_sortformer import _sortformer_feed_wav, _wav_data_ok  # noqa: E402


def _chunk_ids(path: Path) -> list[bytes]:
    header = path.read_bytes()[:256]
    pos = 12
    ids: list[bytes] = []
    while pos + 8 <= len(header):
        cid = header[pos : pos + 4]
        chunk_size = struct.unpack("<I", header[pos + 4 : pos + 8])[0]
        ids.append(cid)
        if cid == b"data":
            break
        pos += 8 + chunk_size + (chunk_size % 2)
    return ids


def test_sortformer_feed_strips_list_chunk(tmp_path: Path) -> None:
    py = ROOT / "venvs" / "nemo-diar" / "Scripts" / "python.exe"
    if not py.is_file():
        pytest.skip("nemo-diar venv missing")

    src = tmp_path / "input.wav"
    run_dir = tmp_path / "run"
    script = (
        "import soundfile as sf\n"
        "import numpy as np\n"
        f"sf.write({str(src)!r}, np.zeros(16000, dtype='float32'), 16000, subtype='PCM_16')\n"
    )
    import subprocess

    subprocess.run([str(py), "-c", script], check=True)

    feed = _sortformer_feed_wav(src, run_dir)
    assert feed.is_file()
    assert _wav_data_ok(feed)
    assert b"LIST" not in _chunk_ids(feed)
    assert b"data" in _chunk_ids(feed)
