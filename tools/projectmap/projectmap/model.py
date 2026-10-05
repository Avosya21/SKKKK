"""Факты, которые разбор достаёт из одного файла: символы, вызовы, импорты.

Все языки приводятся к одной форме, поэтому построение графа, привязка вызовов и метрики от языка не зависят.
Соглашения: символ-модуль имеет `kind == "module"` и `qualname == путь файла`; вызов на уровне модуля — `caller == "<module>"`,
в теле класса — `"Класс.<class>"`; функция, переданная как значение (`Depends(get_db)`), — вызов с `qualifier == "<ref>"`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MODULE_CALLER = "<module>"
REF = "<ref>"


@dataclass
class Symbol:
    name: str
    qualname: str
    kind: str  # module, class, interface, enum, struct, trait, function, method, test, constant
    path: str
    line: int
    end_line: int
    signature: str = ""
    doc: str = ""
    decorators: str = ""
    bases: list[str] = field(default_factory=list)  # для классов: имена базовых классов и интерфейсов
    lang: str = ""


@dataclass
class Call:
    path: str
    caller: str  # квалифицированное имя вызывающего символа или `<module>`
    callee: str  # простое имя вызываемого
    qualifier: str  # что стоит слева от точки (`self`, `people`, `a.b`) или пусто
    line: int


@dataclass
class Import:
    path: str
    module: str  # `a.b`, `.rel`, `./file`, `pkg/sub`: как записано в коде, без нормализации
    names: list[str] = field(default_factory=list)  # импортированные имена (`from x import a, b`)
    aliases: dict[str, str] = field(default_factory=dict)  # локальное имя -> исходное (`import a.b as c`, `from x import a as b`)
    line: int = 0


@dataclass
class FileFacts:
    path: str
    language: str | None = None
    tier: str = "file"
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    lines: int = 0
    error: str = ""  # причина, по которой файл разобран не полностью
