"""Разбор одного файла: выбирает разборщик по языку и возвращает факты (символы, вызовы, импорты).

Python — стандартный `ast`; языки с правилами — общий разбор tree-sitter; остальные — файловый уровень (факты без символов).
Сбой разбора одного файла не должен останавливать построение карты: он попадает в `FileFacts.error`.
"""

from __future__ import annotations

from pathlib import Path

from projectmap.lang_python import extract_python
from projectmap.lang_regex import extract_groovy
from projectmap.lang_specs import spec_for
from projectmap.lang_treesitter import extract_treesitter
from projectmap.languages import FILE, language_of
from projectmap.model import FileFacts


def extract_file(root: Path, path: str, text: str) -> FileFacts:
    language = language_of(path)
    if language is None:
        return FileFacts(path, None, FILE, lines=len(text.splitlines()))
    try:
        if language.id == "python":
            return extract_python(path, text)
        if language.id == "groovy":  # грамматика Groovy непригодна: разбор по регулярным выражениям
            return extract_groovy(path, text, language)
        spec = spec_for(language)
        if spec is None:
            return FileFacts(path, language.id, FILE, lines=len(text.splitlines()), error="правил разбора языка пока нет")
        return extract_treesitter(root, path, text, language, spec)
    except Exception as exc:  # noqa: BLE001 - один неразобранный файл не должен ронять всю карту
        return FileFacts(path, language.id, language.tier, lines=len(text.splitlines()), error=f"сбой разбора: {exc.__class__.__name__}")
