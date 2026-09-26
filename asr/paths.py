"""Рабочие каталоги ASR: из env (prod) или asr/work + asr/out (dev)."""

from __future__ import annotations

import os
from pathlib import Path

ASR = Path(__file__).resolve().parent
ROOT = ASR.parent


def _path(env_key: str, default: Path) -> Path:
    value = os.environ.get(env_key, "").strip()
    return Path(value) if value else default


WORK = _path("SKKKK_ASR_WORK", ASR / "work")
OUT = _path("SKKKK_ASR_OUT", ASR / "out")


def default_audio_url() -> str:
    custom = os.environ.get("SKKKK_AUDIO_URL", "").strip()
    if custom:
        return custom
    wav = WORK / "input_16k.wav"
    return "../work/input_16k.wav" if wav.is_file() else ""
