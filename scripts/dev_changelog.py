"""Журнал разработки из git: docs/CHANGELOG-DEV.md (генерируется, руками не править).

  venv\Scripts\python.exe scripts\dev_changelog.py            # последние 100 коммитов
  venv\Scripts\python.exe scripts\dev_changelog.py --limit 300

Группировка по дням; у каждого коммита — затронутые области (каталоги верхнего уровня) и число файлов.
В конце — незакоммиченные правки по областям. Пригодится, чтобы быстро понять, что менялось, и для ИИ-агентов.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "CHANGELOG-DEV.md"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def area(path: str) -> str:
    parts = path.split("/")
    if parts[0] == "app" and len(parts) > 3:
        return "/".join(parts[:3]) + "/"
    return parts[0] + ("/" if len(parts) > 1 else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()
    raw = git("log", f"-n{args.limit}", "--name-only", "--pretty=format:\x1e%h\x1f%ad\x1f%an\x1f%s", "--date=short")
    lines = ["# Журнал разработки (генерируется)", "",
             "> Создан `scripts/dev_changelog.py` из git; не править руками. Подробности коммита: `git show <хеш>`.", ""]
    day = ""
    for block in raw.split("\x1e"):
        block = block.strip("\n")
        if not block:
            continue
        head, _, files = block.partition("\n")
        h, date, author, subject = (head.split("\x1f") + ["", "", "", ""])[:4]
        flist = [f for f in files.splitlines() if f.strip()]
        if date != day:
            day = date
            lines += [f"## {date}", ""]
        areas = Counter(area(f) for f in flist).most_common(5)
        lines.append(f"- `{h}` {subject} — {author}; файлов {len(flist)}: " + ", ".join(f"{a} ({n})" for a, n in areas))
    status = [l for l in git("status", "--porcelain").splitlines() if l.strip()]
    if status:
        areas = Counter(area(l[3:].strip().strip('"')) for l in status).most_common(12)
        lines += ["", "## Не закоммичено", "", f"Изменённых файлов: {len(status)}. По областям: " + ", ".join(f"{a} ({n})" for a, n in areas)]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"записано: {OUT.relative_to(ROOT)} ({len(lines)} строк)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
