"""Конфиг проекта для projectmap: где граф, что не считать (осознанные исключения) и цели охвата по пробам.

Файл `.projectmap/config.yaml` в корне проекта:

    graph: путь к графу (json), от корня проекта
    ignore: [маски путей, которые карте не нужны: данные, сторонний код, бинарники]
    targets: {проба: минимальная доля охвата, 0..1}
    metrics: {resolution, seeds, hub_min, cochange_min, cochange_max_files, top, misplaced_share, misplaced_min_size}
    groups: {префикс пути: название подсистемы}; без правила подсистема — первый каталог пути
    adapters: [fastapi, sqlalchemy, env, docker, markdown, scripts, web]; пусто — все адаптеры, чей стек есть в проекте
    ignore_env: [маски имён переменных окружения, которые образцу не нужны: системные, тестовые, внутренние]; системные (`PATH`, `HOME`…) пропускаются всегда

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
SYSTEM_ENV = frozenset({"PATH", "PATHEXT", "HOME", "USER", "USERNAME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA",
                        "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "SYSTEMROOT", "WINDIR", "COMPUTERNAME", "HOSTNAME", "SESSIONNAME",
                        "SHELL", "LANG", "LC_ALL", "PWD", "TERM", "CI", "PYTHONPATH", "PYTHONIOENCODING", "PYTEST_CURRENT_TEST", "VIRTUAL_ENV"})


@dataclass
class Config:
    graph: str = str(DEFAULT_GRAPH).replace("\\", "/")
    ignore: list[str] = field(default_factory=list)
    targets: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)  # параметры metrics.MetricsOptions
    groups: dict[str, str] = field(default_factory=dict)  # префикс пути -> подсистема
    adapters: list[str] = field(default_factory=list)  # пусто — автоопределение
    ignore_env: list[str] = field(default_factory=list)  # маски имён переменных окружения вне проверок образца

    def is_env_ignored(self, name: str) -> bool:
        """Системные переменные ОС и перечисленные в `ignore_env` в проверках описания окружения не участвуют (без учёта регистра)."""
        upper = name.upper()
        return upper in SYSTEM_ENV or any(glob_to_regex(p.upper()).match(upper) for p in self.ignore_env)

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
    config.metrics = dict(data.get("metrics") or {})
    config.groups = {str(k).rstrip("/"): str(v) for k, v in (data.get("groups") or {}).items()}
    config.ignore_env = [str(p) for p in data.get("ignore_env") or []]
    adapters = data.get("adapters") or []
    config.adapters = [] if adapters == "auto" else [str(a) for a in adapters]
    return config


def load_config(root: Path, path: Path | None = None) -> Config:
    """Прочитать конфиг проекта; нет файла — значения по умолчанию."""
    file = path or (root / CONFIG_PATH)
    if not file.is_file():
        return Config()
    return parse_config(yaml.safe_load(file.read_text(encoding="utf-8")))
