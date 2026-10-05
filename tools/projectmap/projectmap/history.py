"""История правок из git: коммиты и какие файлы каждый из них менял.

Нужна слою «История» карты (частота правок, пары файлов, меняющихся вместе). Нет git или репозиторий пуст — истории нет, остальная карта
строится без неё.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

RECORD = "\x1e"
FIELD = "\x1f"


@dataclass
class Commit:
    hash: str
    author: str
    date: str
    subject: str
    files: list[str] = field(default_factory=list)


def read_history(root: Path, limit: int = 500) -> list[Commit]:
    """Последние `limit` коммитов (новые первыми) с изменёнными файлами."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "-n", str(limit), "--name-only", "--no-renames",
             f"--format={RECORD}%H{FIELD}%an{FIELD}%aI{FIELD}%s"],
            capture_output=True, check=True).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError):
        return []
    commits: list[Commit] = []
    for block in out.split(RECORD):
        if not block.strip():
            continue
        header, _, rest = block.partition("\n")
        parts = header.split(FIELD)
        if len(parts) < 4:
            continue
        commits.append(Commit(parts[0], parts[1], parts[2], parts[3], [line.strip() for line in rest.splitlines() if line.strip()]))
    return commits
