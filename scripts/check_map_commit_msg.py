"""Проверка сообщения коммита: изменения интерфейса или данных требуют карты или явного отказа от неё.

Вызывается git-хуком `commit-msg` (ставит `scripts/install_hooks.ps1`) с путём к файлу сообщения. Если в коммите
меняются роутеры, модели БД, настройки, шаблоны, JS/CSS страниц или `.env.example`, а `docs/PROJECT-MAP.md` и манифест
карты в коммите не тронуты, в сообщении должна быть строка с причиной:

    Карта: не требуется — <почему, хотя бы несколько слов>

Это не обход, а осознанное решение: правка внутри функции без нового поведения, опечатка, стиль. Если поведение,
кнопка, адрес, таблица или настройка изменились — обновите карту, а не пишите эту строку.

Тесты и документы карту не требуют; слияния и откаты (`Merge`, `Revert`) не проверяются.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Поверхность проекта, которую видит человек или другой ИИ: адреса, данные, настройки, экраны.
SURFACE = (
    re.compile(r"^app/app/routers/.+\.py$"),
    re.compile(r"^app/app/models/.+\.py$"),
    re.compile(r"^app/app/config\.py$"),
    re.compile(r"^app/app/templates/.+\.html$"),
    re.compile(r"^app/app/static/(?!vendor/).+\.(js|css)$"),
    re.compile(r"^\.env\.example$"),
)
MAP_FILES = ("docs/PROJECT-MAP.md", "docs/project-map/manifest.yaml")
NOTE = re.compile(r"^\s*Карта:\s*не требуется\s*[—–-]\s*\S.{3,}$", re.IGNORECASE | re.MULTILINE)

MESSAGE = """
В коммите меняется поверхность проекта (адреса, модели БД, настройки, экраны), а карта проекта не тронута:
{files}

Что сделать:
  • поведение, кнопка, адрес, таблица или настройка изменились — обновите docs/PROJECT-MAP.md
    (правила — §14 карты) и добавьте его в коммит;
  • поведение не менялось (опечатка, стиль, правка внутри функции) — добавьте в сообщение строку:
        Карта: не требуется — <причина>

Не обходите проверку через --no-verify.
""".strip()


def surface_files(paths: list[str]) -> list[str]:
    return [p for p in paths if any(rx.match(p) for rx in SURFACE)]


def needs_note(paths: list[str], message: str) -> list[str]:
    """Файлы поверхности, из-за которых нужна строка о карте; пусто — коммит в порядке."""
    text = "\n".join(line for line in message.splitlines() if not line.startswith("#"))
    first = text.strip().splitlines()[0] if text.strip() else ""
    if first.startswith(("Merge", "Revert")):
        return []
    if any(p in MAP_FILES for p in paths):
        return []
    if NOTE.search(text):
        return []
    return surface_files(paths)


def staged_paths() -> list[str]:
    result = subprocess.run(["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACDMRT"],
                            cwd=ROOT, capture_output=True, check=False)
    if result.returncode != 0:
        return []
    return [p for p in result.stdout.decode("utf-8").split("\0") if p]


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    if len(argv) < 2:
        print("использование: check_map_commit_msg.py <файл сообщения коммита>", file=sys.stderr)
        return 0
    message = Path(argv[1]).read_text(encoding="utf-8", errors="replace")
    files = needs_note(staged_paths(), message)
    if not files:
        return 0
    shown = "\n".join(f"  {p}" for p in files[:12]) + (f"\n  … и ещё {len(files) - 12}" if len(files) > 12 else "")
    print(MESSAGE.format(files=shown), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
