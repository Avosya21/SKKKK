"""Грамматики tree-sitter: локальный кэш проекта, какие языки доступны и как докачать недостающие.

Грамматики скачиваются один раз в каталог `.projectmap/grammars` проекта (не в git); после этого разбор работает без интернета.
Python разбирается стандартным `ast` и в грамматике не нуждается. Нет пакета `tree-sitter-language-pack` или грамматики —
язык остаётся на файловом уровне, остальной разбор продолжается.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

CACHE_DIR = Path(".projectmap") / "grammars"


class GrammarUnavailable(RuntimeError):
    """Нет пакета разбора или грамматики конкретного языка."""


@dataclass
class GrammarStatus:
    ready: set[str] = field(default_factory=set)
    failed: dict[str, str] = field(default_factory=dict)  # язык -> причина


def _pack():
    try:
        import tree_sitter_language_pack as pack
    except ImportError as exc:  # пакет не установлен
        raise GrammarUnavailable("не установлен пакет tree-sitter-language-pack") from exc
    return pack


def configure(root: Path) -> Path:
    """Направить кэш грамматик в каталог проекта; возвращает его путь."""
    pack = _pack()
    cache = (root / CACHE_DIR).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    pack.configure(pack.PackConfig(cache_dir=str(cache)))
    return cache


def downloaded(root: Path) -> set[str]:
    """Грамматики, уже лежащие в кэше проекта."""
    pack = _pack()
    configure(root)
    return set(pack.downloaded_languages())


def ensure(root: Path, languages: list[str], *, download: bool = True) -> GrammarStatus:
    """Подготовить грамматики: недостающие докачать (если разрешено). Ошибка одной грамматики не мешает остальным."""
    status = GrammarStatus()
    pack = _pack()
    configure(root)
    have = set(pack.downloaded_languages())
    status.ready |= {lang for lang in languages if lang in have}
    for lang in [lang for lang in languages if lang not in have]:
        if not download:
            status.failed[lang] = "нет в кэше"
            continue
        try:
            pack.download([lang])
            status.ready.add(lang)
        except Exception as exc:  # noqa: BLE001 - сеть, неизвестный язык, повреждённый кэш
            status.failed[lang] = f"{exc.__class__.__name__}: {str(exc)[:120]}"
    return status


def parser_for(root: Path, language: str):
    """Разборщик tree-sitter для языка; GrammarUnavailable, если грамматики нет."""
    pack = _pack()
    configure(root)
    try:
        return pack.get_parser(language)
    except Exception as exc:  # noqa: BLE001
        raise GrammarUnavailable(f"нет грамматики {language}: {exc.__class__.__name__}") from exc
