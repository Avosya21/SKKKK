"""Ядро projectmap не знает конкретного проекта: в его коде нет названий SKKKK, путей и файлов нашего портала.

Это проверка общности без второго проекта (план, «Проверка общности»): всё специфичное идёт через адаптеры и конфиг.
"""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "projectmap"

# слова, которые выдают привязку к SKKKK; проверяются без учёта регистра
FORBIDDEN = (
    r"skkkk", r"app/app", r"app\\app", r"project_map\.db", r"login_up", r"panel_users", r"companies\.txt",
    r"lm ?studio", r"livekit", r"gigaam", r"parakeet", r"nemotron",
)


def test_package_has_python_files():
    assert list(PACKAGE.glob("*.py")), PACKAGE


def test_core_code_mentions_no_specific_project():
    pattern = re.compile("|".join(FORBIDDEN), re.IGNORECASE)
    hits = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(PACKAGE.parent)}:{number}: {line.strip()[:80]}")
    assert not hits, "ядро привязано к проекту:\n" + "\n".join(hits)
