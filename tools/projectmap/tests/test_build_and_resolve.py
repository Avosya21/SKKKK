"""Сборка графа ядром и привязка импортов и вызовов: многоязычный проект во временной папке."""

from __future__ import annotations

from pathlib import Path

import pytest

from projectmap import grammars
from projectmap.build import build_graph, file_id, group_of, symbol_id
from projectmap.config import Config, parse_config
from projectmap.imports import FileIndex, resolve_import
from projectmap.model import Import
from projectmap.schema import validate_graph

ROOT = Path(__file__).resolve().parents[3]  # корень репозитория: общий кэш грамматик

FILES = {
    "py/models.py": "class Base:\n    pass\n\nclass User(Base):\n    def persist(self):\n        pass\n\ndef helper():\n    pass\n",
    "py/other.py": "def close():\n    pass\n\ndef helper():\n    pass\n",
    "py/service.py": ("from py.models import User, helper as h\nfrom py import models\n\n\ndef create():\n    u = User()\n    u.persist()\n"
                      "    h()\n    models.helper()\n    conn.close()\n    unknown_call()\n"),
    "tests/test_service.py": "from py.service import create\n\n\ndef test_create():\n    create()\n",
    "web/a.js": "import { b } from './b';\nexport function a() { b(); }\n",
    "web/b.js": "export function b() {}\n",
    "src/com/x/A.java": "package com.x;\nimport com.x.B;\nclass A extends B { void m() { B.s(); helper(); } }\n",
    "src/com/x/B.java": "package com.x;\nclass B { static void s() {} void helper() {} }\n",
    "go/pkg/a.go": "package pkg\nfunc A() { B() }\n",
    "go/pkg/b.go": "package pkg\nfunc B() {}\n",
    "docs/readme.md": "текст\n",
}


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    try:
        status = grammars.ensure(ROOT, ["java", "go", "javascript"])
    except grammars.GrammarUnavailable as exc:
        pytest.skip(str(exc))
    if {"java", "go", "javascript"} - status.ready:
        pytest.skip("нет грамматик (нет сети и нет кэша)")
    root = tmp_path_factory.mktemp("project")
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    patch = pytest.MonkeyPatch()
    patch.setenv(grammars.CACHE_ENV, str(grammars.configure(ROOT)))  # грамматики берём из общего кэша репозитория
    try:
        yield build_graph(root, Config())
    finally:
        patch.undo()


def _edges(graph, rel):
    return {(e["from"], e["to"]): e for e in graph["edges"] if e["rel"] == rel}


def test_graph_is_valid_and_has_files_and_symbols(graph):
    assert [p for p in validate_graph(graph) if p.severity == "error"] == []
    ids = {n["id"] for n in graph["nodes"]}
    assert {file_id(p) for p in FILES} <= ids
    assert symbol_id("py/models.py", "User.persist") in ids and symbol_id("go/pkg/b.go", "B") in ids
    assert graph["stats"]["by_kind"]["file"] == len(FILES) and graph["source"]["tool"] == "projectmap"


def test_python_calls_resolve_through_imports_aliases_and_modules(graph):
    calls = _edges(graph, "calls")
    create = symbol_id("py/service.py", "create")
    assert calls[(create, symbol_id("py/models.py", "User"))]["how"] == "import"
    assert calls[(create, symbol_id("py/models.py", "helper"))]["n"] == 2  # `h()` через алиас и `models.helper()` через модуль
    assert {calls[(create, symbol_id("py/models.py", "helper"))]["how"]} <= {"import", "module"}
    assert (create, symbol_id("py/other.py", "helper")) not in calls, "одноимённая функция из другого файла не должна подмешаться"


def test_common_method_on_unknown_object_and_unknown_calls_make_no_edge(graph):
    targets = {e["to"] for e in graph["edges"] if e["from"] == symbol_id("py/service.py", "create") and e["rel"] == "calls"}
    assert symbol_id("py/other.py", "close") not in targets and not any("unknown_call" in t for t in targets)


def test_method_named_by_unique_name_on_object_is_linked_as_unique(graph):
    calls = _edges(graph, "calls")
    edge = calls[(symbol_id("py/service.py", "create"), symbol_id("py/models.py", "User.persist"))]
    assert edge["how"] == "unique"


def test_test_files_cover_code_and_do_not_add_calls(graph):
    covers = _edges(graph, "covers")
    assert (symbol_id("tests/test_service.py", "test_create"), symbol_id("py/service.py", "create")) in covers
    assert not [e for e in graph["edges"] if e["rel"] == "calls" and e["from"].startswith("sym:tests/")]


def test_inheritance_is_resolved_across_python_and_java(graph):
    inherits = _edges(graph, "inherits")
    assert (symbol_id("py/models.py", "User"), symbol_id("py/models.py", "Base")) in inherits
    assert (symbol_id("src/com/x/A.java", "A"), symbol_id("src/com/x/B.java", "B")) in inherits


def test_imports_between_files_are_edges_with_counts(graph):
    imports = _edges(graph, "imports")
    assert (file_id("py/service.py"), file_id("py/models.py")) in imports and imports[(file_id("py/service.py"), file_id("py/models.py"))]["n"] == 2
    assert (file_id("tests/test_service.py"), file_id("py/service.py")) in imports
    assert (file_id("web/a.js"), file_id("web/b.js")) in imports and (file_id("src/com/x/A.java"), file_id("src/com/x/B.java")) in imports


def test_javascript_java_and_go_calls_resolve_by_their_own_rules(graph):
    calls = _edges(graph, "calls")
    assert calls[(symbol_id("web/a.js", "a"), symbol_id("web/b.js", "b"))]["how"] == "import"
    assert calls[(symbol_id("src/com/x/A.java", "A.m"), symbol_id("src/com/x/B.java", "B.s"))]["how"] == "module"
    assert calls[(symbol_id("go/pkg/a.go", "A"), symbol_id("go/pkg/b.go", "B"))]["how"] == "package"


def test_no_call_crosses_between_language_families(graph):
    nodes = {n["id"]: n for n in graph["nodes"]}
    for edge in graph["edges"]:
        if edge["rel"] in ("calls", "calls_ref"):
            a, b = nodes[edge["from"]]["path"], nodes[edge["to"]]["path"]
            assert a.rsplit(".", 1)[-1] == b.rsplit(".", 1)[-1] or {a.rsplit(".", 1)[-1], b.rsplit(".", 1)[-1]} <= {"java"}, (a, b)


def test_groups_follow_the_first_directory_or_config_rules():
    assert group_of("py/a.py", Config()) == "py" and group_of("README.md", Config()) == "корень"
    config = parse_config({"groups": {"src": "исходники", "src/com": "пакет"}})
    assert group_of("src/main.py", config) == "исходники" and group_of("src/com/x.py", config) == "пакет" and group_of("lib/x.py", config) == "lib"


def test_ignored_paths_are_not_in_the_graph(tmp_path):
    (tmp_path / "keep.py").write_text("def a():\n    pass\n", encoding="utf-8")
    (tmp_path / "skip").mkdir()
    (tmp_path / "skip" / "x.py").write_text("def b():\n    pass\n", encoding="utf-8")
    graph = build_graph(tmp_path, parse_config({"ignore": ["skip/**"]}))
    assert {n["path"] for n in graph["nodes"] if n["kind"] == "file"} == {"keep.py"}


def test_build_is_deterministic(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    g()\n\ndef g():\n    pass\n", encoding="utf-8")
    first, second = build_graph(tmp_path), build_graph(tmp_path)
    for graph in (first, second):
        graph["built_at"] = 0
        graph["source"]["seconds"] = 0
    assert first == second


# ───────────────────────────── привязка импортов по языкам ─────────────────────────────

def _targets(importer, imp, files):
    return {(t.local, t.original, t.file, t.is_module) for t in resolve_import(importer, imp, FileIndex(files))}


def test_python_relative_and_suffix_imports():
    files = {"pkg/a.py", "pkg/sub/__init__.py", "pkg/sub/c.py", "other/a.py"}
    assert _targets("pkg/b.py", Import("pkg/b.py", ".a", ["f"]), files) == {("f", "f", "pkg/a.py", False)}
    assert _targets("pkg/b.py", Import("pkg/b.py", ".", ["sub"]), files) == {("sub", None, "pkg/sub/__init__.py", True)}
    assert _targets("pkg/sub/c.py", Import("pkg/sub/c.py", "..a", ["g"]), files) == {("g", "g", "pkg/a.py", False)}
    assert _targets("x.py", Import("x.py", "pkg.sub.c", [], {"cc": "pkg.sub.c"}), files) == {("cc", None, "pkg/sub/c.py", True), ("pkg.sub.c", None, "pkg/sub/c.py", True)}
    assert _targets("x.py", Import("x.py", "os.path", []), files) == set()


def test_python_ambiguous_module_is_resolved_only_by_proximity():
    files = {"one/util.py", "two/util.py", "one/main.py"}
    assert _targets("one/main.py", Import("one/main.py", "util", ["f"]), files) == {("f", "f", "one/util.py", False)}
    assert _targets("elsewhere.py", Import("elsewhere.py", "util", ["f"]), files) == set()


def test_javascript_relative_extension_and_index_resolution():
    files = {"web/a.ts", "web/lib/index.js", "web/lib/x.tsx"}
    assert {t.file for t in resolve_import("web/main.js", Import("web/main.js", "./a", ["f"]), FileIndex(files))} == {"web/a.ts"}
    assert {t.file for t in resolve_import("web/main.js", Import("web/main.js", "./lib", []), FileIndex(files))} == {"web/lib/index.js"}
    assert resolve_import("web/main.js", Import("web/main.js", "react", []), FileIndex(files)) == []


def test_go_package_import_gives_every_file_of_the_directory():
    files = {"go/pkg/a.go", "go/pkg/b.go", "go/pkg/b_test.go", "go/other/c.go", "go/main.go"}
    targets = resolve_import("go/main.go", Import("go/main.go", "pkg", [], {"p": "pkg"}), FileIndex(files))
    assert {(t.local, t.file) for t in targets} == {("p", "go/pkg/a.go"), ("p", "go/pkg/b.go")}


def test_rust_c_and_script_imports():
    assert {t.file for t in resolve_import("src/main.rs", Import("src/main.rs", "crate::util::tools", []), FileIndex({"src/util/tools.rs", "src/main.rs"}))} == {"src/util/tools.rs"}
    assert resolve_import("src/main.rs", Import("src/main.rs", "std::io", ["Read"]), FileIndex({"src/main.rs"})) == []
    assert {t.file for t in resolve_import("src/a.c", Import("src/a.c", "my.h", []), FileIndex({"src/my.h", "src/a.c"}))} == {"src/my.h"}
    assert {t.file for t in resolve_import("run.sh", Import("run.sh", "./lib.sh", []), FileIndex({"lib.sh", "run.sh"}))} == {"lib.sh"}
    assert {t.file for t in resolve_import("run.rb", Import("run.rb", "./lib", []), FileIndex({"lib.rb", "run.rb"}))} == {"lib.rb"}
    assert {t.file for t in resolve_import("a.lua", Import("a.lua", "util.str", []), FileIndex({"util/str.lua", "a.lua"}))} == {"util/str.lua"}


def test_java_class_and_wildcard_imports():
    files = {"src/com/x/B.java", "src/com/x/C.java", "src/com/y/D.java"}
    assert {(t.local, t.file) for t in resolve_import("src/com/y/D.java", Import("src/com/y/D.java", "com.x.B", []), FileIndex(files))} == {("B", "src/com/x/B.java")}
    assert {t.file for t in resolve_import("src/com/y/D.java", Import("src/com/y/D.java", "com.x.*", []), FileIndex(files))} == {"src/com/x/B.java", "src/com/x/C.java"}
    assert resolve_import("src/com/y/D.java", Import("src/com/y/D.java", "java.util.List", []), FileIndex(files)) == []
    assert {t.file for t in resolve_import("src/com/y/D.java", Import("src/com/y/D.java", "com.x.B.helper", []), FileIndex(files))} == {"src/com/x/B.java"}
