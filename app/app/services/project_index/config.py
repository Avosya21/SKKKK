"""Конфигурация индекса проекта: какие файлы читать, что исключать, как прятать секреты."""

from __future__ import annotations

import fnmatch
import os
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

# Разбор чужого кода не должен сыпать предупреждениями об экранировании в строках проекта.
warnings.filterwarnings("ignore", message="invalid escape sequence")
ROOT = Path(__file__).resolve().parents[4]
# PROJECT_INDEX_DB — другой файл индекса (эксперименты и тесты); лок-файл лежит рядом.
DB_PATH = Path(os.environ.get("PROJECT_INDEX_DB") or ROOT / "data" / "project_map.db")
LOCK_PATH = DB_PATH.with_suffix(".lock")
CONFIG_PATH = ROOT / "scripts" / "index_config.yaml"
GLOSSARY_PATH = ROOT / "scripts" / "index_glossary.yaml"
LOG_PATH = ROOT / "data" / "logs" / "index.log"
SCHEMA_VERSION = 2
# Поднимать при любом изменении логики нарезки (chunkers.py, analyzers.py): файлы перечитаются, векторы возьмутся из кэша.
CHUNKER_VERSION = "7"  # 7: алиасы символов symbol_aliases


@dataclass
class IndexConfig:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    never_index: list[str] = field(default_factory=list)
    always_allow: list[str] = field(default_factory=list)
    max_file_bytes: int = 1_000_000
    max_lines: int = 120
    embed_chars: int = 800
    history_commits: int = 500


def load_config() -> IndexConfig:
    import yaml

    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.is_file() else {}
    chunk = raw.get("chunk") or {}
    return IndexConfig(
        include=list(raw.get("include") or []),
        exclude=list(raw.get("exclude") or []),
        never_index=list(raw.get("never_index") or []),
        always_allow=list(raw.get("always_allow") or []),
        max_file_bytes=int(raw.get("max_file_bytes") or 1_000_000),
        max_lines=int(chunk.get("max_lines") or 120),
        embed_chars=int(chunk.get("embed_chars") or 800),
        history_commits=int(chunk.get("history_commits") or 500),
    )


def load_glossary() -> dict[str, list[str]]:
    import yaml

    if not GLOSSARY_PATH.is_file():
        return {}
    data = yaml.safe_load(GLOSSARY_PATH.read_text(encoding="utf-8")) or {}
    return {str(k).lower(): [str(x) for x in v] for k, v in data.items()}


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`**` — любая вложенность (в том числе нулевая), `*` — без слэша."""
    out = ""
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif ch == "*":
            out += "[^/]*"
            i += 1
        elif ch == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(ch)
            i += 1
    return re.compile("^" + out + "$")


class Matcher:
    def __init__(self, cfg: IndexConfig) -> None:
        self.include = [_glob_to_regex(p) for p in cfg.include]
        self.exclude = [_glob_to_regex(p) for p in cfg.exclude]
        self.never = cfg.never_index
        self.allow = set(cfg.always_allow)
        self.max_bytes = cfg.max_file_bytes

    def wanted(self, rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        if name not in self.allow and any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat) for pat in self.never):
            return False
        if any(rx.match(rel) for rx in self.exclude):
            return False
        return any(rx.match(rel) for rx in self.include)


def list_files(cfg: IndexConfig | None = None) -> list[str]:
    """Относительные пути (со слэшами) всех индексируемых файлов."""
    import os

    cfg = cfg or load_config()
    matcher = Matcher(cfg)
    skip_dirs = {".git", "venv", "venvs", "vendor", "node_modules", "data", "tmp", "logs", ".idea",
                 ".pytest_cache", "__pycache__", "faster-whisper-large-v3-turbo-ct2", "nvidiadiar_sortformer_4spk-v1",
                 "pyannotesegmentation3.0", "speaker-diarization-3.1", "wespeaker-voxceleb-resnet34-LM",
                 "whisper-large-v3-ru-podlodka", "android", "ffmpeg", "libreoffice", "tesseract"}
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = Path(dirpath).relative_to(ROOT).as_posix()
        # app/app/data — каталог данных, но `data` мы отсекаем везде, кроме корня проекта нигде не нужен
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith("gigaam-") and d not in {"out", "experiments"}]
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if not matcher.wanted(rel):
                continue
            try:
                if (Path(dirpath) / name).stat().st_size > cfg.max_file_bytes:
                    continue
            except OSError:
                continue
            found.append(rel)
    return sorted(found)


_SECRET_LITERAL = re.compile(
    r"""(?ix)((?:api_?key|token|secret|passwd|password|pwd|private_?key|access_?key)\w*\s*[:=]\s*)(["'])([^"'\n]{12,})\2"""
)
_SECRET_ENVLINE = re.compile(r"(?im)^(\s*\w*(?:KEY|TOKEN|SECRET|PASSWORD|HASH)\w*\s*=\s*)(\S{8,})\s*$")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{20,}")


def redact(text: str, *, env_like: bool = False) -> str:
    """Спрятать значения секретов: в индекс попадает имя, но не значение."""
    text = _SECRET_LITERAL.sub(lambda m: f"{m.group(1)}{m.group(2)}***{m.group(2)}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)}***", text)
    if env_like:
        text = _SECRET_ENVLINE.sub(lambda m: f"{m.group(1)}***", text)
    return text
