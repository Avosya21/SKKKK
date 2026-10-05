"""Реестр языков: расширения файлов, уровень поддержки и имя грамматики tree-sitter.

Уровни: `deep` — глубокий разбор и знание фреймворков (Python, JS/TS), `structural` — символы, импорты, вызовы и наследование
по общему правилу tree-sitter, `file` — только файл. Язык, которого нет в реестре, остаётся на файловом уровне.
"""

from __future__ import annotations

from dataclasses import dataclass

DEEP = "deep"
STRUCTURAL = "structural"
FILE = "file"


@dataclass(frozen=True)
class Language:
    id: str  # имя грамматики tree-sitter
    title: str
    extensions: tuple[str, ...]
    tier: str


LANGUAGES: tuple[Language, ...] = (
    Language("python", "Python", (".py", ".pyi"), DEEP),
    Language("javascript", "JavaScript", (".js", ".mjs", ".cjs", ".jsx"), DEEP),
    Language("typescript", "TypeScript", (".ts", ".mts", ".cts"), DEEP),
    Language("tsx", "TypeScript (JSX)", (".tsx",), DEEP),
    Language("java", "Java", (".java",), STRUCTURAL),
    Language("kotlin", "Kotlin", (".kt", ".kts"), STRUCTURAL),
    Language("go", "Go", (".go",), STRUCTURAL),
    Language("rust", "Rust", (".rs",), STRUCTURAL),
    Language("c", "C", (".c", ".h"), STRUCTURAL),
    Language("cpp", "C++", (".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"), STRUCTURAL),
    Language("csharp", "C#", (".cs",), STRUCTURAL),
    Language("ruby", "Ruby", (".rb",), STRUCTURAL),
    Language("php", "PHP", (".php",), STRUCTURAL),
    Language("swift", "Swift", (".swift",), STRUCTURAL),
    Language("groovy", "Groovy", (".groovy", ".gradle"), STRUCTURAL),
    Language("objc", "Objective-C", (".m", ".mm"), STRUCTURAL),
    Language("scala", "Scala", (".scala",), STRUCTURAL),
    Language("dart", "Dart", (".dart",), STRUCTURAL),
    Language("lua", "Lua", (".lua",), STRUCTURAL),
    Language("bash", "Shell", (".sh", ".bash"), STRUCTURAL),
    Language("powershell", "PowerShell", (".ps1", ".psm1"), STRUCTURAL),
)

BY_ID = {language.id: language for language in LANGUAGES}
BY_EXTENSION = {ext: language for language in LANGUAGES for ext in language.extensions}


def language_of(path: str) -> Language | None:
    """Язык файла по расширению; None — язык не поддержан (файл остаётся на файловом уровне)."""
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return None
    return BY_EXTENSION.get("." + name.rsplit(".", 1)[-1].lower())


def tier_of(path: str) -> str:
    language = language_of(path)
    return language.tier if language else FILE


def grammar_ids() -> list[str]:
    """Грамматики tree-sitter, нужные для всех структурных языков (Python разбирается стандартным `ast`)."""
    return [language.id for language in LANGUAGES if language.id != "python"]
