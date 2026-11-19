"""Смоук обёртка: делегирует scripts/e2e/e2e_meeting_record.py."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "e2e" / "e2e_meeting_record.py"
raise SystemExit(subprocess.call([sys.executable, str(TARGET)], cwd=str(TARGET.parents[2])))
