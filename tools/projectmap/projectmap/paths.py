"""Пути проекта: признаки, общие для проб и метрик (тестовый ли файл)."""

from __future__ import annotations


def is_test_path(path: str) -> bool:
    """Файл тестов по пути: каталог `tests`/`test` или имя вида `test_*.py`, `*_test.py`, `*.test.js`, `*.spec.ts`."""
    parts = path.split("/")
    name = parts[-1]
    return ("tests" in parts or "test" in parts or name.startswith("test_")
            or name.endswith(("_test.py", ".test.js", ".spec.js", ".test.ts", ".spec.ts")))
