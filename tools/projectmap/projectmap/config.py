"""Конфиг проекта для projectmap: где граф, что не считать (осознанные исключения) и цели охвата по пробам.

Файл `.projectmap/config.yaml` в корне проекта:

    graph: путь к графу (json), от корня проекта
    ignore: [маски путей, которые карте не нужны: данные, сторонний код, бинарники]
    targets: {проба: минимальная доля охвата, 0..1}

Маски: `*` — любые символы кроме `/`, `**` — любые символы включая `/`, `?` — один символ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(".projectmap") / "config.yaml"
DEFAULT_GRAPH = Path(".projectmap") / "out" / "graph.json"


@dataclass
class Config:
    graph: str = str(DEFAULT_GRAPH).replace("\\", "/")
    ignore: list[str] = field(default_factory=list)
    targets: dict[str, float] = field(default_factory=dict)

    def is_ignored(self, rel_path: str) -> bool:
        return any(glob_to_regex(pattern).match(rel_path) for pattern in self.ignore)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Маска пути -> регулярное выражение. `dir/**` захватывает всё внутри каталога и сам каталог."""
    out = ""
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        if ch == "*":
            out += "[^/]*"
        elif ch == "?":
            out += "[^/]"
        else:
            out += re.escape(ch)
        i += 1
    return re.compile(out + r"\Z")


def parse_config(data: dict[str, Any] | None) -> Config:
    data = data or {}
    config = Config()
    if data.get("graph"):
        config.graph = str(data["graph"])
    config.ignore = [str(p) for p in data.get("ignore") or []]
    config.targets = {str(k): float(v) for k, v in (data.get("targets") or {}).items()}
    return config


def load_config(root: Path, path: Path | None = None) -> Config:
    """Прочитать конфиг проекта; нет файла — значения по умолчанию."""
    file = path or (root / CONFIG_PATH)
    if not file.is_file():
        return Config()
    return parse_config(yaml.safe_load(file.read_text(encoding="utf-8")))
