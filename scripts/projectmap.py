"""Запуск инструмента projectmap из репозитория: `venv\Scripts\python.exe scripts\projectmap.py check`.

Добавляет `tools/projectmap` в путь поиска модулей и вызывает его командную строку; пакет не нужно ставить в окружение.
Команда `report` пересобирает документ охвата карты `docs/project-map/COVERAGE.md` (по графу `data/project_map_graph.json`).
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "projectmap"))

from projectmap.cli import main  # noqa: E402

REPORT = ROOT / "docs" / "project-map" / "COVERAGE.md"
HEAD = """# Охват карты проекта

Сгенерировано `venv\Scripts\python.exe scripts\projectmap.py report` ({stamp}). Не править руками.

**Что это.** Для каждого слоя карты инструмент `projectmap` отдельно находит, что есть в проекте, и считает, сколько из этого
есть в графе зависимостей (`data/project_map_graph.json`). Это измеримая полнота карты: цели заданы в `.projectmap/config.yaml`,
осознанные исключения (сторонний код, данные, архив) — там же, с причинами. Строка ✗ — охват ниже цели.
Подробности и план закрытия пробелов — `docs/plans/PROJECTMAP-PLAN.md`.

"""


def report() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        body = Path(tmp) / "body.md"
        code = main(["check", "--root", str(ROOT), "--format", "markdown", "--output", str(body)])
        if code:
            return code
        text = HEAD.format(stamp=time.strftime("%Y-%m-%d %H:%M")) + body.read_text(encoding="utf-8")
    REPORT.write_text(text, encoding="utf-8", newline="")
    print(f"записано: {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "report":
        raise SystemExit(report())
    if args and args[0] == "check" and "--root" not in args:
        args += ["--root", str(ROOT)]
    raise SystemExit(main(args))
