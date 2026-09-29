"""Чтение и ресемпл WAV для стадий лаборатории.

Поиск ffmpeg совпадает с app.app.services.ffmpeg_tools, но без импорта FastAPI:
venvs\\lab не содержит pydantic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import numpy as np

SAMPLE_RATE_16K = 16_000
SAMPLE_RATE_48K = 48_000

_WIN_FFMPEG = (
    Path(r"C:\ffmpeg-8.0-full_build\bin"),
    Path(r"C:\ffmpeg\bin"),
)


def resolve_ffmpeg() -> str:
    env_dir = os.environ.get("FFMPEG_DIR")
    if env_dir:
        candidate = Path(env_dir) / "ffmpeg.exe"
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("ffmpeg")
    if found:
        return found
    for base in _WIN_FFMPEG:
        candidate = base / "ffmpeg.exe"
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        "ffmpeg не найден; задайте FFMPEG_DIR или поставьте в C:\\ffmpeg-8.0-full_build\\bin"
    )


def resample_wav(src: Path, dst: Path, sample_rate: int) -> Path:
    """Моно PCM 16 бит на заданной частоте. Перезапись всегда — файл в прогоне."""
    ffmpeg = resolve_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le",
            str(dst),
        ],
        check=True,
    )
    return dst


def read_mono(path: Path) -> tuple[np.ndarray, int]:
    import soundfile as sf

    data, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    return data.mean(axis=1).astype(np.float32, copy=False), int(sample_rate)


def write_mono(path: Path, samples: np.ndarray, sample_rate: int) -> Path:
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.asarray(samples, dtype=np.float32), sample_rate, subtype="PCM_16")
    return path


def duration_sec(path: Path) -> float:
    import soundfile as sf

    info = sf.info(str(path))
    return float(info.frames) / float(info.samplerate or 1)


def rms_db(samples: np.ndarray) -> float:
    """Уровень сигнала в дБFS. Нужен для A/B до и после денойза."""
    arr = np.asarray(samples, dtype=np.float64)
    if arr.size == 0:
        return -120.0
    value = float(np.sqrt(np.mean(np.square(arr))))
    if value < 1e-9:
        return -120.0
    return round(20.0 * float(np.log10(value)), 2)


def frame_levels_db(path: Path, frame_sec: float) -> np.ndarray:
    """Громкость каждого кадра записи в дБFS. Файл читается блоками, целиком в память не ложится."""
    import soundfile as sf

    hop = max(1, int(round(frame_sec * sf.info(str(path)).samplerate)))
    levels: list[np.ndarray] = []
    for block in sf.blocks(str(path), blocksize=hop * 4096, dtype="float32", always_2d=True):
        mono = block.mean(axis=1, dtype=np.float64)
        frames = [mono[: mono.size // hop * hop].reshape(-1, hop)]
        if mono.size % hop:
            frames.append(mono[mono.size // hop * hop :].reshape(1, -1))
        for chunk in frames:
            rms = np.sqrt(np.mean(np.square(chunk), axis=1))
            levels.append(20.0 * np.log10(np.maximum(rms, 1e-6)))
    return np.concatenate(levels) if levels else np.zeros(0)


def noise_floor_db(samples: np.ndarray, sample_rate: int, *, percentile: float = 10.0) -> float:
    """Оценка шумовой полки: RMS тихих 20-мс окон.

    Грубая, но сравнимая между «до» и «после» — этого для A/B достаточно.
    """
    arr = np.asarray(samples, dtype=np.float64)
    win = max(1, int(0.02 * sample_rate))
    usable = (arr.size // win) * win
    if usable < win:
        return rms_db(arr)
    frames = arr[:usable].reshape(-1, win)
    energies = np.sqrt(np.mean(np.square(frames), axis=1))
    quiet = float(np.percentile(energies, percentile))
    if quiet < 1e-9:
        return -120.0
    return round(20.0 * float(np.log10(quiet)), 2)
