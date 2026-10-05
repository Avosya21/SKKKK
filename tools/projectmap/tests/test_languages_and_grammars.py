"""Реестр языков и грамматики tree-sitter: расширения, уровни, кэш проекта и разбор простого кода."""

from __future__ import annotations

from pathlib import Path

import pytest

from projectmap import grammars
from projectmap.languages import BY_ID, DEEP, FILE, LANGUAGES, STRUCTURAL, grammar_ids, language_of, tier_of

ROOT = Path(__file__).resolve().parents[3]  # корень репозитория: кэш грамматик общий для всех проверок


def test_extensions_map_to_languages_and_tiers():
    assert language_of("src/app.py").id == "python" and tier_of("src/app.py") == DEEP
    assert language_of("web/App.TSX").id == "tsx" and language_of("a/b/main.go").id == "go"
    assert language_of("tools/run.ps1").id == "powershell" and tier_of("lib/Util.java") == STRUCTURAL
    assert language_of("README") is None and language_of("data.json") is None and tier_of("data.json") == FILE


def test_every_extension_belongs_to_exactly_one_language():
    seen: dict[str, str] = {}
    for language in LANGUAGES:
        for ext in language.extensions:
            assert ext not in seen, f"{ext}: {seen[ext]} и {language.id}"
            seen[ext] = language.id
    assert set(BY_ID) == {lang.id for lang in LANGUAGES}


def test_python_needs_no_grammar_but_the_others_do():
    ids = grammar_ids()
    assert "python" not in ids and {"java", "go", "javascript", "powershell"} <= set(ids)


def test_baseline_languages_are_all_supported():
    for ext in (".c", ".cpp", ".groovy", ".java", ".js", ".ts", ".kt", ".m", ".rb", ".swift", ".py", ".go"):
        assert any(ext in language.extensions for language in LANGUAGES), ext


@pytest.fixture(scope="module")
def ready():
    try:
        status = grammars.ensure(ROOT, ["go", "java"])
    except grammars.GrammarUnavailable as exc:
        pytest.skip(str(exc))
    if {"go", "java"} - status.ready:
        pytest.skip(f"грамматики не доступны (нет сети и нет кэша): {status.failed}")
    return status


def test_grammars_live_in_the_project_cache(ready):
    cache = grammars.configure(ROOT)
    assert cache == (ROOT / grammars.CACHE_DIR).resolve() and cache.is_dir()
    assert {"go", "java"} <= grammars.downloaded(ROOT)


def test_parser_parses_code_without_errors(ready):
    tree = grammars.parser_for(ROOT, "go").parse(b"package main\nfunc a() { b() }\n")
    assert not tree.root_node.has_error and tree.root_node.type == "source_file"


def test_missing_language_is_reported_not_raised_when_download_is_off():
    status = grammars.ensure(ROOT, ["definitely-not-a-language"], download=False)
    assert "definitely-not-a-language" in status.failed and not status.ready
