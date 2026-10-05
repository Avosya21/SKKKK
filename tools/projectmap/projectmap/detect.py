"""Определение стека проекта для `projectmap init`: языки по расширениям, фреймворки и среда по признакам, что стоит исключить из карты."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from projectmap.context import tracked_files
from projectmap.languages import language_of
from projectmap.paths import is_test_path

# каталоги и маски, которые почти всегда не нужны карте: зависимости, сборка, кэши, минифицированные и сгенерированные файлы
COMMON_IGNORE = (
    ("**/node_modules/**", "сторонние пакеты"), ("**/vendor/**", "сторонний код"), ("**/dist/**", "результат сборки"),
    ("**/build/**", "результат сборки"), ("**/.venv/**", "виртуальное окружение"), ("**/venv/**", "виртуальное окружение"),
    ("**/__pycache__/**", "кэш Python"), ("**/*.min.js", "минифицированный код"), ("**/*.min.css", "минифицированные стили"),
    ("**/package-lock.json", "файл блокировки зависимостей"), ("**/yarn.lock", "файл блокировки зависимостей"),
    ("**/*.lock", "файл блокировки зависимостей"), ("**/*.map", "карты исходников сборки"),
)
BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp", ".pdf", ".zip", ".gz", ".tar", ".woff", ".woff2", ".ttf",
                   ".eot", ".mp3", ".mp4", ".wav", ".bin", ".onnx", ".pt", ".db", ".sqlite", ".exe", ".dll", ".pyc")
FRAMEWORK_MARKERS = (  # (идентификатор, подпись, расширения файлов с текстом, подстроки в тексте)
    ("fastapi", "FastAPI / Starlette", (".py",), ("fastapi", "starlette")),
    ("sqlalchemy", "SQLAlchemy", (".py",), ("sqlalchemy",)),
    ("flask", "Flask", (".py",), ("from flask", "import flask")),
    ("django", "Django", (".py",), ("django.",)),
    ("express", "Express", (".js", ".ts"), ("require('express')", 'require("express")', "from 'express'", 'from "express"')),
    ("react", "React", (".jsx", ".tsx", ".js"), ("from 'react'", 'from "react"', "require('react')")),
)
SAMPLE_FILES = 400  # сколько файлов читать при поиске признаков фреймворков


def detect_stack(root: Path) -> dict[str, Any]:
    files = tracked_files(root)
    languages: Counter = Counter()
    for path in files:
        language = language_of(path)
        if language:
            languages[language.id] += 1
    code = [p for p in files if language_of(p)]
    frameworks: list[str] = []
    for ident, title, suffixes, needles in FRAMEWORK_MARKERS:
        for path in [p for p in code if p.endswith(suffixes)][:SAMPLE_FILES]:
            try:
                text = (root / path).read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                continue
            if any(n in text for n in needles):
                frameworks.append(title)
                break
    names = {Path(p).name.lower() for p in files}
    ignore = [(mask, why) for mask, why in COMMON_IGNORE if any(_hits(mask, p) for p in files)]
    present = sorted({"." + p.rsplit(".", 1)[-1].lower() for p in files if p.lower().endswith(BINARY_SUFFIXES)})
    ignore += [(f"**/*{suffix}", "бинарные данные") for suffix in present]
    binaries = sum(1 for p in files if p.lower().endswith(BINARY_SUFFIXES))
    return {
        "files": len(files), "languages": dict(languages.most_common()), "frameworks": frameworks,
        "docker": any(n.startswith("docker-compose") or n in ("compose.yml", "compose.yaml", "dockerfile") for n in names),
        "docs": sum(1 for p in files if p.lower().endswith((".md", ".rst"))),
        "tests": sum(1 for p in files if is_test_path(p) and language_of(p)),
        "has_git": (root / ".git").exists(), "ignore": ignore, "binaries": binaries,
        "map_files": [p for p in files if p.lower() in ("docs/project-map.md", "project-map.md", "docs/architecture.md", "architecture.md")],
    }


def _hits(mask: str, path: str) -> bool:
    from projectmap.config import glob_to_regex

    return bool(glob_to_regex(mask).match(path))
