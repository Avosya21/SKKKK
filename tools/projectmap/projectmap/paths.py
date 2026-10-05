"""Пути проекта: признаки, общие для проб и метрик (тестовый ли файл)."""

from __future__ import annotations


SCRIPT_SUFFIXES = (".py", ".ps1", ".sh", ".bat", ".cmd")


def unique_script_names(paths) -> dict[str, str]:
    """Имя файла -> путь для скриптов, чьё имя достаточно уникально, чтобы искать его в тексте (не короче восьми знаков, с разделителем, один файл)."""
    import re

    by_name: dict[str, list[str]] = {}
    for path in paths:
        if not path.endswith(SCRIPT_SUFFIXES) or is_test_path(path):
            continue
        name = path.rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0]
        if len(name) >= 8 and re.search(r"[_.\-]", stem) and stem != "__init__":
            by_name.setdefault(name, []).append(path)
    return {name: items[0] for name, items in by_name.items() if len(items) == 1}


def is_test_path(path: str) -> bool:
    """Файл тестов по пути: каталог `tests`/`test` или имя вида `test_*.py`, `*_test.py`, `*.test.js`, `*.spec.ts`."""
    parts = path.split("/")
    name = parts[-1]
    return ("tests" in parts or "test" in parts or name.startswith("test_")
            or name.endswith(("_test.py", ".test.js", ".spec.js", ".test.ts", ".spec.ts")))
