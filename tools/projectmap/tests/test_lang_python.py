"""Разбор Python: символы, вызовы по принадлежности, импорты (в том числе внутри функций), базовые классы, ошибки синтаксиса."""

from __future__ import annotations

from projectmap.lang_python import extract_python

SOURCE = '''"""Модуль платежей. Вторая строка."""
import os
import a.b as ab
from pkg.mod import helper as h, other
from . import sibling
from ..up import thing
from star import *

TOKEN_TTL = 4
lower = 5
AB = 1

class Box(Base, Generic[T], mod.Mixin, metaclass=Meta):
    """Коробка."""
    LIMIT: int = 3
    ready = print("in class body")

    @decorated
    def run(self, x):
        y = h(x)
        os.path.join("a", "b")
        def inner():
            return other()
        from lazy.mod import late
        return self.helper(y)

    class Inner:
        def deep(self):
            return thing()

async def top(a):
    return Box().run(a)

if True:
    def conditional():
        sibling.go()

print(top(1))
register(top)
'''


def _facts():
    return extract_python("pkg/m.py", SOURCE)


def _symbols(facts):
    return {s.qualname: s for s in facts.symbols}


def test_symbols_cover_module_constants_classes_methods_and_functions():
    symbols = _symbols(_facts())
    assert symbols["pkg/m.py"].kind == "module" and symbols["pkg/m.py"].name == "m" and symbols["pkg/m.py"].doc == "Модуль платежей. Вторая строка."
    assert {k: v.kind for k, v in symbols.items() if k != "pkg/m.py"} == {
        "TOKEN_TTL": "constant", "Box": "class", "Box.run": "method", "Box.Inner": "class", "Box.Inner.deep": "method", "top": "function"}
    assert "lower" not in symbols and "AB" not in symbols  # константа — ВЕРХНИЙ_РЕГИСТР длиннее двух букв
    assert symbols["Box.run"].decorators == "decorated" and symbols["Box.run"].signature == "def run(self, x):"
    assert symbols["Box.run"].line == 18, "строка символа — по первому декоратору"


def test_class_bases_drop_generics_and_keywords():
    assert _symbols(_facts())["Box"].bases == ["Base", "Generic", "mod.Mixin"]


def test_calls_belong_to_the_right_caller():
    calls = {(c.caller, c.callee, c.qualifier) for c in _facts().calls}
    assert ("Box.run", "h", "") in calls and ("Box.run", "join", "os.path") in calls and ("Box.run", "helper", "self") in calls
    assert ("Box.run", "other", "") in calls, "вызов в замыкании принадлежит функции"
    assert ("Box.<class>", "print", "") not in calls, "print — встроенная функция"
    assert ("Box.Inner.deep", "thing", "") in calls and ("top", "run", "Box()") in calls and ("top", "Box", "") in calls
    assert ("<module>", "register", "") in calls and ("<module>", "go", "sibling") in calls, "определение внутри if принадлежит модулю"


def test_function_passed_as_a_value_is_a_reference():
    calls = {(c.caller, c.callee, c.qualifier) for c in _facts().calls}
    assert ("<module>", "top", "<ref>") in calls and ("<module>", "top", "") in calls


def test_imports_include_nested_ones_aliases_and_relative_levels():
    imports = {i.module: i for i in _facts().imports}
    assert set(imports) == {"os", "a.b", "pkg.mod", ".", "..up", "star", "lazy.mod"}
    assert imports["a.b"].aliases == {"ab": "a.b"} and imports["pkg.mod"].names == ["helper", "other"] and imports["pkg.mod"].aliases == {"h": "helper"}
    assert imports["."].names == ["sibling"] and imports["star"].names == []
    assert imports["lazy.mod"].line > imports["pkg.mod"].line


def test_import_alias_of_a_module_is_recorded():
    facts = extract_python("m.py", "import a.b as ab\nimport c\n")
    assert [(i.module, i.aliases) for i in facts.imports] == [("a.b", {"ab": "a.b"}), ("c", {})]


def test_functions_in_test_files_are_tests():
    facts = extract_python("tests/test_x.py", "def test_a():\n    run()\n\nclass TestB:\n    def test_c(self):\n        run()\n")
    assert {s.qualname: s.kind for s in facts.symbols if s.kind != "module"} == {"test_a": "test", "TestB": "class", "TestB.test_c": "test"}


def test_syntax_error_keeps_the_module_symbol_and_reports_the_reason():
    facts = extract_python("broken.py", "def f(:\n    pass\n")
    assert [s.kind for s in facts.symbols] == ["module"] and "синтаксическая ошибка" in facts.error and not facts.calls


def test_calls_are_ordered_by_line_and_deterministic():
    first, second = _facts(), _facts()
    assert first.calls == second.calls and [c.line for c in first.calls] == sorted(c.line for c in first.calls)
