"""Isolated venv for the LiveKit voice recorder (tools/livekit/voice_cleaner.py). Not the app venv.

Шумодав созвона работает в браузере (DFN3), рекордеру нужен только livekit SDK.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / "venvs" / "voice"


def _py() -> Path:
    return VENV / "Scripts" / "python.exe"


def main() -> int:
    VENV.mkdir(parents=True, exist_ok=True)
    if not _py().exists():
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    pip = [str(_py()), "-m", "pip", "install", "--upgrade", "pip"]
    subprocess.check_call(pip)
    subprocess.check_call([str(_py()), "-m", "pip", "install", "livekit"])
    print("voice venv ready:", _py())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
