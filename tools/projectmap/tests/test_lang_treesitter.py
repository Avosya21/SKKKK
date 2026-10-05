"""Разбор не-Python языков через tree-sitter: образец кода на каждый язык и ожидаемые символы, вызовы, импорты, базовые классы."""

from __future__ import annotations

from pathlib import Path

import pytest

from projectmap import grammars
from projectmap.extract import extract_file
from projectmap.lang_specs import CAPABILITIES, SPECS
from projectmap.lang_treesitter import split_target
from projectmap.languages import LANGUAGES, grammar_ids

ROOT = Path(__file__).resolve().parents[3]  # корень репозитория: кэш грамматик общий

# язык -> (файл, код, символы, подмножество вызовов, модули импортов, базы классов)
CASES = {
    "javascript": ("src/a.js",
        "import a, {b as bb} from './a';\nconst d = require('./d');\nclass K extends B { m(x){ this.n(x); obj.o(1); } }\nfunction top(){ plain(); }\n"
        "const arrow = () => y();\nexport default function ex(){}\n",
        {("class", "K"), ("method", "K.m"), ("function", "top"), ("function", "arrow"), ("function", "ex")},
        {("K.m", "n", "this"), ("K.m", "o", "obj"), ("top", "plain", ""), ("arrow", "y", "")}, {"./a", "./d"}, {"K": ["B"]}),
    "typescript": ("src/a.ts",
        "import { x, y as z } from './x';\ninterface I extends J { m(): void }\nclass K extends B implements I { m(): void { this.n(); helper(); } }\nenum E { A }\n"
        "function top(): void { plain(); new Foo(); }\nconst f = (a: number) => g(a);\n",
        {("interface", "I"), ("class", "K"), ("method", "K.m"), ("enum", "E"), ("function", "top"), ("function", "f")},
        {("K.m", "helper", ""), ("K.m", "n", "this"), ("top", "plain", ""), ("top", "Foo", ""), ("f", "g", "")}, {"./x"}, {"I": ["J"], "K": ["B", "I"]}),
    "tsx": ("src/a.tsx",
        "import React from 'react';\nexport function Comp(props: P) { return <div>{render()}</div>; }\nclass W extends React.Component { render() { return null; } }\n",
        {("function", "Comp"), ("class", "W"), ("method", "W.render")},
        {("Comp", "render", "")}, {"react"}, {"W": ["React.Component"]}),
    "powershell": ("src/a.ps1",
        "function Get-Thing { Other-It -a 1 }\n. ./lib.ps1\nImport-Module Foo\nGet-Thing\n",
        {("function", "Get-Thing")},
        {("<module>", "Get-Thing", "")}, {"./lib.ps1", "Foo"}, {}),
    "java": ("src/A.java",
        "package a.b;\nimport java.util.List;\nimport static x.Y.z;\npublic class K extends B implements I, J {\n  K() { super(); }\n"
        "  void m() { n(); obj.o(1); this.p(); }\n  class In { void q() {} }\n}\ninterface I { void z(); }\nenum E { A, B }\n",
        {("class", "K"), ("method", "K.K"), ("method", "K.m"), ("class", "K.In"), ("method", "K.In.q"), ("interface", "I"), ("method", "I.z"), ("enum", "E")},
        {("K.m", "n", ""), ("K.m", "o", "obj"), ("K.m", "p", "this")}, {"java.util.List", "x.Y.z"}, {"K": ["B", "I", "J"]}),
    "go": ("src/a.go",
        "package main\nimport (\n \"fmt\"\n m \"pkg/m\"\n)\ntype S struct{ Base }\ntype I interface{ Do() }\n"
        "func (s *S) Run() { fmt.Println(); s.other() }\nfunc top() { m.Do(); plain() }\n",
        {("struct", "S"), ("interface", "I"), ("method", "S.Run"), ("function", "top")},
        {("S.Run", "Println", "fmt"), ("S.Run", "other", "s"), ("top", "Do", "m"), ("top", "plain", "")}, {"fmt", "pkg/m"}, {}),
    "rust": ("src/a.rs",
        "use std::io::{self, Read};\nuse crate::a::b;\nstruct S;\ntrait T { fn t(&self); }\nimpl T for S { fn t(&self) { helper(); self.go(); a::b::c(); } }\n"
        "fn top() { plain(); }\nenum E { A }\n",
        {("struct", "S"), ("trait", "T"), ("method", "T.t"), ("method", "S.t"), ("function", "top"), ("enum", "E")},
        {("S.t", "helper", ""), ("S.t", "go", "self"), ("S.t", "c", "a::b"), ("top", "plain", "")}, {"std::io", "crate::a::b"}, {"S": ["T"]}),
    "kotlin": ("src/a.kt",
        "package a\nimport b.C\nimport d.E as F\nclass K : Base(), I { fun m() { n(); o.p(1) } }\nfun top() { plain() }\n",
        {("class", "K"), ("method", "K.m"), ("function", "top")},
        {("K.m", "n", ""), ("K.m", "p", "o"), ("top", "plain", "")}, {"b.C", "d.E"}, {"K": ["Base", "I"]}),
    "csharp": ("src/a.cs",
        "using System;\nusing A.B;\nnamespace N { class K : Base, I { void M() { N(); obj.O(1); this.P(); } }\n interface I { void Z(); } enum E { A } struct S {} }\n",
        {("class", "K"), ("method", "K.M"), ("interface", "I"), ("method", "I.Z"), ("enum", "E"), ("struct", "S")},
        {("K.M", "N", ""), ("K.M", "O", "obj"), ("K.M", "P", "this")}, {"System", "A.B"}, {"K": ["Base", "I"]}),
    "c": ("src/a.c",
        "#include <stdio.h>\n#include \"my.h\"\nstruct S { int a; };\nint helper(int x);\nint top(int a) { helper(a); printf(\"x\"); obj.fn(); p->go(); return 0; }\n",
        {("struct", "S"), ("function", "top")},
        {("top", "helper", ""), ("top", "fn", "obj"), ("top", "go", "p")}, {"stdio.h", "my.h"}, {}),
    "cpp": ("src/a.cpp",
        "#include <vector>\n#include \"x.hpp\"\nnamespace ns { class K : public Base { public: void m() { n(); obj.o(); this->p(); a::b(); } }; }\n"
        "int K::q() { return 1; }\nvoid top() { plain(); }\n",
        {("class", "K"), ("method", "K.m"), ("method", "K.q"), ("function", "top")},
        {("K.m", "n", ""), ("K.m", "o", "obj"), ("K.m", "p", "this"), ("K.m", "b", "a"), ("top", "plain", "")}, {"vector", "x.hpp"}, {"K": ["Base"]}),
    "ruby": ("src/a.rb",
        "require 'json'\nrequire_relative './lib'\nmodule M\n  class K < Base\n    def m(x)\n      n(x)\n      obj.o(1)\n    end\n  end\nend\ndef top\n  plain\nend\n",
        {("class", "M.K"), ("method", "M.K.m"), ("function", "top")},
        {("M.K.m", "n", ""), ("M.K.m", "o", "obj")}, {"json", "./lib"}, {"M.K": ["Base"]}),
    "php": ("src/a.php",
        "<?php\nnamespace A;\nuse B\\C;\nuse D\\E as F;\nrequire 'x.php';\nclass K extends B implements I { function m() { n(); $o->p(); self::q(); C::r(); } }\n"
        "interface I { function z(); }\nfunction top() { plain(); }\n",
        {("class", "K"), ("method", "K.m"), ("interface", "I"), ("method", "I.z"), ("function", "top")},
        {("K.m", "n", ""), ("K.m", "p", "$o"), ("K.m", "q", "self"), ("K.m", "r", "C"), ("top", "plain", "")}, {"B\\C", "D\\E", "x.php"}, {"K": ["B", "I"]}),
    "swift": ("src/a.swift",
        "import Foundation\nclass K: Base, P { func m() { n(); obj.o(1); self.p() } }\nprotocol P { func z() }\nstruct S {}\nenum E { case a }\nfunc top() { plain() }\n",
        {("class", "K"), ("method", "K.m"), ("interface", "P"), ("method", "P.z"), ("struct", "S"), ("enum", "E"), ("function", "top")},
        {("K.m", "n", ""), ("K.m", "o", "obj"), ("K.m", "p", "self"), ("top", "plain", "")}, {"Foundation"}, {"K": ["Base", "P"]}),
    "scala": ("src/a.scala",
        "import a.b.C\nclass K extends B with T { def m() = { n(); obj.o(1) } }\nobject O { def s() = 1 }\ntrait T { def z(): Int }\ndef top() = plain()\n",
        {("class", "K"), ("method", "K.m"), ("class", "O"), ("method", "O.s"), ("trait", "T"), ("method", "T.z"), ("function", "top")},
        {("K.m", "n", ""), ("K.m", "o", "obj"), ("top", "plain", "")}, {"a.b.C"}, {"K": ["B", "T"]}),
    "lua": ("src/a.lua",
        "local a = require('a')\nlocal M = {}\nfunction M.m(x) n(x); obj.o(1); obj:p() end\nfunction top() plain() end\n",
        {("method", "M.m"), ("function", "top")},
        {("M.m", "n", ""), ("M.m", "o", "obj"), ("M.m", "p", "obj"), ("top", "plain", "")}, {"a"}, {}),
    "bash": ("src/a.sh",
        "source ./lib.sh\n. ./other.sh\nfunction f1() { helper arg; ./run.sh; }\nf2() { cmd; }\nf1\n",
        {("function", "f1"), ("function", "f2")},
        {("f1", "helper", ""), ("f1", "run.sh", ""), ("f2", "cmd", ""), ("<module>", "f1", "")}, {"./lib.sh", "./other.sh"}, {}),
    "objc": ("src/a.m",
        "#import <Foundation/Foundation.h>\n#import \"My.h\"\n@interface K : Base\n- (void)m;\n@end\n@implementation K\n- (void)m { [self n]; [obj o:1]; plain(); }\n@end\n"
        "void top() { plain(); }\n",
        {("class", "K"), ("method", "K.m"), ("function", "top")},
        {("K.m", "n", "self"), ("K.m", "o", "obj"), ("K.m", "plain", ""), ("top", "plain", "")}, {"Foundation/Foundation.h", "My.h"}, {"K": ["Base"]}),
    "dart": ("src/a.dart",
        "import 'package:a/a.dart';\nimport 'b.dart' as bb;\nclass K extends B implements I { void m() { n(); } }\nvoid top() { plain(); }\n",
        {("class", "K"), ("method", "K.m"), ("function", "top")},
        set(), {"package:a/a.dart", "b.dart"}, {"K": ["B", "I"]}),
    "groovy": ("src/a.groovy",
        "import a.b.C\n\nclass K extends B implements I {\n    void m() {\n        n()\n    }\n    String name() {\n        return 'x'\n    }\n}\n\ndef top() {\n    plain()\n}\n",
        {("class", "K"), ("method", "K.m"), ("method", "K.name"), ("function", "top")},
        set(), {"a.b.C"}, {"K": ["B", "I"]}),
}


@pytest.fixture(scope="module", autouse=True)
def grammars_ready():
    try:
        status = grammars.ensure(ROOT, grammar_ids())
    except grammars.GrammarUnavailable as exc:
        pytest.skip(str(exc))
    missing = set(grammar_ids()) - status.ready
    if missing:
        pytest.skip(f"нет грамматик (нет сети и нет кэша): {sorted(missing)}")


@pytest.mark.parametrize("language", sorted(CASES))
def test_language_sample_gives_expected_facts(language):
    path, source, symbols, calls, imports, bases = CASES[language]
    facts = extract_file(ROOT, path, source)
    assert facts.language == language and facts.symbols[0].kind == "module" and facts.symbols[0].qualname == path
    assert {(s.kind, s.qualname) for s in facts.symbols if s.kind != "module"} == symbols
    assert calls <= {(c.caller, c.callee, c.qualifier) for c in facts.calls}
    assert {i.module for i in facts.imports} == imports
    assert {s.qualname: s.bases for s in facts.symbols if s.bases} == bases


def test_every_supported_language_has_a_sample_and_capabilities():
    supported = {lang.id for lang in LANGUAGES if not lang.native}
    assert supported == set(CASES), "на каждый язык реестра нужен образец"
    assert set(SPECS) | {"groovy"} == supported
    assert set(CAPABILITIES) == supported and all(set(v) <= set("scib") and "s" in v for v in CAPABILITIES.values())


def test_languages_without_calls_say_so_in_capabilities():
    assert "c" not in CAPABILITIES["dart"] and "c" not in CAPABILITIES["groovy"]
    assert all("c" in CAPABILITIES[lang] for lang in ("java", "go", "rust", "javascript", "kotlin"))


def test_functions_in_test_files_are_tests():
    facts = extract_file(ROOT, "src/test/FooTest.java", "class FooTest { void testA() { run(); } }\n")
    assert {s.qualname: s.kind for s in facts.symbols if s.kind != "module"} == {"FooTest": "class", "FooTest.testA": "test"}


def test_closure_belongs_to_the_enclosing_function_and_has_no_symbol():
    facts = extract_file(ROOT, "a.js", "function outer() { const inner = () => helper(); inner(); }\n")
    assert [s.qualname for s in facts.symbols if s.kind != "module"] == ["outer"]
    assert {(c.caller, c.callee) for c in facts.calls} == {("outer", "helper"), ("outer", "inner")}


def test_noise_calls_are_skipped():
    facts = extract_file(ROOT, "a.go", "package main\nfunc f() { make([]int, 1); len(x); real() }\n")
    assert [c.callee for c in facts.calls] == ["real"]
    shell = extract_file(ROOT, "a.sh", "f() { echo hi; cd /tmp; mine; }\n")
    assert [c.callee for c in shell.calls] == ["mine"]


def test_unknown_extension_stays_at_file_level():
    facts = extract_file(ROOT, "notes/readme.xyz", "text\n")
    assert facts.language is None and facts.tier == "file" and not facts.symbols


def test_missing_grammar_does_not_stop_the_extraction(monkeypatch):
    def broken(root, language):
        raise grammars.GrammarUnavailable("нет грамматики test")

    monkeypatch.setattr(grammars, "parser_for", broken)
    facts = extract_file(ROOT, "a.go", "package main\nfunc f() {}\n")
    assert facts.error == "нет грамматики test" and not facts.symbols and facts.language == "go"


def test_a_crash_in_one_file_is_reported_not_raised(monkeypatch):
    import projectmap.extract as extract

    def boom(*args, **kwargs):
        raise ValueError("x")

    monkeypatch.setattr(extract, "extract_treesitter", boom)
    assert "сбой разбора: ValueError" in extract_file(ROOT, "a.go", "package main\n").error


def test_split_target_handles_separators_and_rejects_expressions():
    assert split_target("foo") == ("foo", "") and split_target("a.b.c") == ("c", "a.b") and split_target("a::b::c") == ("c", "a::b")
    assert split_target("p->go") == ("go", "p") and split_target("obj?.run") == ("run", "obj") and split_target("List<int>.of") == ("of", "List<int>")
    assert split_target("(f)()") is None and split_target("") is None and split_target('"s".len') is None


def test_syntax_errors_are_marked_but_facts_are_kept():
    facts = extract_file(ROOT, "a.go", "package main\nfunc ok() {}\nfunc broken( {\n")
    assert "не разобрала" in facts.error and any(s.qualname == "ok" for s in facts.symbols)


def test_results_are_deterministic():
    path, source = CASES["java"][:2]
    assert extract_file(ROOT, path, source) == extract_file(ROOT, path, source)


def test_javascript_namespace_objects_give_method_symbols_and_resolvable_calls():
    source = ("const NS = {\n  token() { return 1; },\n  async api(path) { return helper(path); },\n  load: () => NS.api('/x'),\n  version: 3,\n};\n"
              "NS.extra = function () { return NS.token(); };\nwindow.G = {};\nwindow.G.run = () => NS.load();\nmodule.exports.exported = function () {};\n")
    facts = extract_file(ROOT, "ns.js", source)
    assert {(s.kind, s.qualname) for s in facts.symbols if s.kind != "module"} == {
        ("method", "NS.token"), ("method", "NS.api"), ("method", "NS.load"), ("method", "NS.extra"), ("method", "G.run"), ("function", "exported")}
    calls = {(c.caller, c.callee, c.qualifier) for c in facts.calls}
    assert ("NS.api", "helper", "") in calls and ("NS.load", "api", "NS") in calls and ("NS.extra", "token", "NS") in calls
