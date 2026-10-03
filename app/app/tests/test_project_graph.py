"""Граф зависимостей проекта: покрытие всех сущностей, связи, окрестности, сироты, циклы, выгрузки.

Тесты сборки идут на настоящем репозитории (индекс собирается в временную БД без эмбеддингов),
поэтому ловят и расхождение графа с кодом: у каждого файла и роута обязан быть узел, висячих рёбер нет.
"""

from __future__ import annotations

import json

import pytest

from app.app.services.project_index import graph as G
from app.app.services.project_index import graph_atlas as A
from app.app.services.project_index import indexer, store


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Индекс репозитория в временной БД + собранный из него граф (без эмбеддингов, быстро)."""
    tmp = tmp_path_factory.mktemp("graphidx")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(store, "DB_PATH", tmp / "map.db")
    monkey.setattr(store, "LOCK_PATH", tmp / "map.lock")
    monkey.setattr(indexer, "LOG_PATH", tmp / "index.log")
    indexer.run_index(full=True, embed=False, echo=False)
    conn = store.connect()
    graph = G.build(conn)
    yield conn, graph
    conn.close()
    monkey.undo()


def _tiny_index(tmp_path, files: dict[str, list[tuple]], *, calls=(), imports=(), routes=()):
    """Крошечный индекс для проверок, которые удобнее делать на синтетике."""
    conn = store.connect(tmp_path / "tiny.db", create=True)
    conn.executemany("INSERT INTO files(path, sha, size, mtime, kind) VALUES (?, 'x', 1, 1, ?)",
                     [(path, path.rsplit(".", 1)[-1]) for path in files])
    for path, symbols in files.items():
        conn.executemany("INSERT INTO symbols(name, qualname, kind, path, line, end_line, lang) "
                         "VALUES (?, ?, ?, ?, ?, ?, 'py')", [(s[0], s[1], s[2], path, s[3], s[4]) for s in symbols])
    for row in calls:
        conn.execute("INSERT INTO calls(path, caller, callee, qualifier, line) VALUES (?, ?, ?, ?, ?)", row)
    for row in imports:
        conn.execute("INSERT INTO imports(path, module, names, line) VALUES (?, ?, ?, ?)", row)
    for row in routes:
        conn.execute("INSERT INTO routes(method, url, handler, path, line, deps, guards, doc) VALUES (?,?,?,?,?,?,?,?)", row)
    conn.commit()
    return conn


# ───────────────────────────── полный граф репозитория ─────────────────────────────

def test_every_file_and_route_has_a_node(built):
    conn, graph = built
    ids = {n["id"] for n in graph["nodes"]}
    files = [r[0] for r in conn.execute("SELECT path FROM files").fetchall()]
    missing = [p for p in files if G._file_id(p) not in ids]
    assert not missing, f"в графе нет файлов: {missing[:5]}"
    routes = conn.execute("SELECT method, url FROM routes").fetchall()
    missing_routes = [f'{r["method"]} {r["url"]}' for r in routes
                      if G._node_id("route", f'{r["method"]} {r["url"]}') not in ids]
    assert not missing_routes, f"в графе нет роутов: {missing_routes[:5]}"


def test_graph_has_no_dangling_edges_and_unique_ids(built):
    _conn, graph = built
    ids = [n["id"] for n in graph["nodes"]]
    assert len(ids) == len(set(ids)), "дубли узлов"
    known = set(ids)
    dangling = [e for e in graph["edges"] if e["from"] not in known or e["to"] not in known]
    assert not dangling, f"висячие рёбра: {dangling[:3]}"
    for node in graph["nodes"]:
        assert node["kind"] and node["label"]
    for edge in graph["edges"]:
        assert edge["rel"] and edge["n"] >= 1


def test_all_layers_are_present(built):
    _conn, graph = built
    rels = graph["stats"]["by_relation"]
    assert rels.get("defines", 0) > 1000, "не связаны файлы и символы"
    assert rels.get("calls", 0) > 1000, "не собраны вызовы"
    assert rels.get("imports", 0) > 100, "не собраны импорты"
    assert rels.get("covers", 0) > 100, "не собрано покрытие тестами"
    assert rels.get("reads", 0) + rels.get("writes", 0) > 50, "не связаны таблицы БД"
    assert rels.get("handles", 0) > 50, "не связаны роуты и обработчики"
    assert rels.get("changed", 0) > 0, "нет истории по файлам"
    kinds = graph["stats"]["by_kind"]
    assert kinds.get("route", 0) > 50 and kinds.get("table", 0) > 10 and kinds.get("ui", 0) > 20
    assert any(rel.startswith("m:") for rel in rels), "не подмешаны смысловые рёбра манифеста"


def test_routes_carry_guards_and_tables(built):
    conn, graph = built
    index = G.node_index(graph)
    row = conn.execute("SELECT method, url, guards, path, handler FROM routes WHERE guards != '' LIMIT 1").fetchone()
    assert row, "в индексе нет роутов с проверкой прав"
    node_id = G._node_id("route", f'{row["method"]} {row["url"]}')
    guards = [index[e["to"]]["label"] for e in graph["edges"] if e["from"] == node_id and e["rel"] == "guarded_by"]
    assert guards, f'у роута {row["url"]} нет связи с проверкой прав'
    handler = [index[e["to"]]["label"] for e in graph["edges"] if e["from"] == node_id and e["rel"] == "handles"]
    assert handler == [row["handler"]], f'роут ведёт не к своему обработчику: {handler}'


def test_neighbors_returns_center_and_links(built):
    _conn, graph = built
    focus = next(n for n in graph["nodes"] if n["kind"] == "symbol" and n.get("sub") in {"function", "method"})
    result = G.neighbors(graph, focus["path"], depth=1, limit=200)
    assert result["center"] is not None
    assert result["nodes"] and all(n["id"] in {x["id"] for x in result["nodes"]} for n in result["nodes"])
    for edge in result["edges"]:
        assert edge["from"] == result["center"]["id"] or edge["to"] == result["center"]["id"] or edge["n"] >= 1
    assert "вызывает" in A.neighbors_text(result) or "вызывают" in A.neighbors_text(result)


def test_orphans_are_really_uncalled(built):
    conn, _graph = built
    rows = G.orphans(conn, limit=20)
    for row in rows[:5]:
        callers = conn.execute("SELECT COUNT(*) FROM calls WHERE callee=?", (row["name"],)).fetchone()[0]
        handlers = conn.execute("SELECT COUNT(*) FROM routes WHERE handler=?", (row["name"],)).fetchone()[0]
        assert callers == 0 and handlers == 0
        assert not row["name"].startswith("__")
    # правило упоминаний: имя не должно встречаться в КОДЕ другого файла (документация находку не отменяет)
    marks = ",".join("?" * len(G.MENTION_CHUNK_KINDS))
    names = {row["name"]: row["path"] for row in rows}
    for chunk in conn.execute(f"SELECT path, text FROM chunks WHERE kind IN ({marks})", G.MENTION_CHUNK_KINDS):
        for name in names:
            if name in (chunk["text"] or ""):
                assert names[name] == chunk["path"], f"{name} упоминается в коде другого файла"


def test_import_cycles_are_consistent(built):
    conn, _graph = built
    components = G.import_cycles(conn, limit=10)
    for component in components:
        assert len(component) > 1, "в циклы попал одиночный файл"
        for path in component:
            assert conn.execute("SELECT COUNT(*) FROM files WHERE path=?", (path,)).fetchone()[0] == 1


def test_exports_are_valid(built):
    _conn, graph = built
    modules = G.module_graph(graph)
    assert modules["nodes"] and all(n["kind"] == "file" for n in modules["nodes"])
    assert all(e["rel"] in {"calls", "imports", "covers", "data", "manifest", "history", "other"} for e in modules["edges"])
    assert json.loads(G.to_json(graph))["stats"]["nodes"] == graph["stats"]["nodes"]
    graphml = G.to_graphml(graph)
    assert graphml.startswith("<?xml") and "<graphml" in graphml and "<node id=" in graphml and "<edge id=" in graphml
    dot = G.to_dot(graph)
    assert dot.startswith("digraph") and "->" in dot
    assert G.to_dot(graph, modules_only=True).count("->") > 0
    summary = G.modules_summary(graph)
    assert summary["subsystems"] and summary["files"] and summary["edges"]
    assert summary["stats"]["nodes"] == graph["stats"]["nodes"]
    assert all({"name", "files", "out", "in"} <= set(row) for row in summary["subsystems"])
    assert all({"from", "to", "rel", "n"} <= set(row) for row in summary["edges"])


def test_atlas_has_all_sections_and_keeps_handwritten_head(tmp_path, built):
    conn, graph = built
    text = A.atlas(graph, conn)
    for section in ("## 1.", "## 4.", "## 8.", "## 9.", "## 10.", "## 11.", "## 12."):
        assert section in text, f"в атласе нет раздела {section}"
    assert A.BEGIN in text and A.END in text
    target = tmp_path / "DEPENDENCIES.md"
    target.write_text("# Рукописная шапка\n\n" + A.BEGIN + "\nстарое\n" + A.END + "\n", encoding="utf-8")
    A.write_atlas(text, target)
    written = target.read_text(encoding="utf-8")
    assert written.startswith("# Рукописная шапка"), "атлас затёр рукописную шапку"
    assert A.atlas(graph, conn, routes_limit=3).count("\n") > 10


def test_summary_text_reports_sources(built):
    _conn, graph = built
    text = A.summary_text(graph)
    assert "узлов" in text and "рёбер" in text and "индекс от" in text


# ───────────────────────────── синтетика: вызовы, внешние, циклы ─────────────────────────────

def test_calls_resolve_locally_and_externals_are_optional(tmp_path):
    conn = _tiny_index(
        tmp_path,
        {"a.py": [("a.py", "a.py", "module", 1, 10), ("caller", "caller", "function", 2, 4),
                  ("helper", "helper", "function", 6, 8)],
         "b.py": [("b.py", "b.py", "module", 1, 5)]},
        calls=[("a.py", "caller", "helper", "", 3), ("a.py", "caller", "print", "", 3),
               ("a.py", "caller", "log_activity", "log", 3)],
    )
    without = G.build(conn, include_manifest=False, include_history=False)
    external_nodes = [n for n in without["nodes"] if n["kind"] == "external"]
    assert not external_nodes, "внешние узлы появились без флага"
    calls = [e for e in without["edges"] if e["rel"] == "calls"]
    assert len(calls) == 1 and calls[0]["to"] == G._symbol_id("a.py", "helper") and calls[0]["line"] == 3
    with_external = G.build(conn, include_external=True, include_manifest=False, include_history=False)
    assert {n["label"] for n in with_external["nodes"] if n["kind"] == "external"} == {"print", "log_activity"}
    assert [o["name"] for o in G.orphans(conn, limit=10)] == ["caller"], "сироты считаются неверно"
    conn.close()


def test_route_links_handler_guard_and_table(tmp_path):
    conn = _tiny_index(
        tmp_path,
        {"r.py": [("r.py", "r.py", "module", 1, 20), ("endpoint", "endpoint", "function", 3, 8),
                  ("require_project", "require_project", "function", 10, 12)]},
        routes=[("GET", "/x", "endpoint", "r.py", 3, "get_db", "require_project", "doc")],
    )
    conn.executemany("INSERT INTO db_columns(tbl, col, ctype, source, path, line) VALUES (?,?,?,?,?,?)",
                     [("t1", "id", "INTEGER", "model", "r.py", 1)])
    conn.execute("INSERT INTO table_access(tbl, path, symbol, line, writes) VALUES ('t1', 'r.py', 'endpoint', 4, 1)")
    conn.commit()
    graph = G.build(conn, include_manifest=False, include_history=False)
    route = G._node_id("route", "GET /x")
    edges = {(e["rel"], e["to"]) for e in graph["edges"] if e["from"] == route}
    assert ("handles", G._symbol_id("r.py", "endpoint")) in edges
    assert ("guarded_by", G._symbol_id("r.py", "require_project")) in edges
    assert ("writes", G._node_id("table", "t1")) in edges, "роут не связан с таблицей через обработчик"
    conn.close()


def test_documentation_mentions_do_not_hide_dead_code(tmp_path):
    """Регрессия: сгенерированный атлас со списком сирот не должен гасить находку на следующей индексации."""
    conn = _tiny_index(tmp_path, {"a.py": [("a.py", "a.py", "module", 1, 10), ("dead_one", "dead_one", "function", 2, 4)]})
    conn.execute("INSERT INTO chunks(path, kind, title, text) VALUES ('docs/x.md', 'doc', 'x', 'dead_one упоминается в документации')")
    conn.commit()
    assert [o["name"] for o in G.orphans(conn, limit=10)] == ["dead_one"], "документация не должна отменять находку"
    conn.execute("INSERT INTO chunks(path, kind, title, text) VALUES ('b.py', 'function', 'f', 'dead_one()')")
    conn.commit()
    assert G.orphans(conn, limit=10) == [], "упоминание в коде другого файла отменяет находку"
    conn.close()


def test_import_cycle_detected(tmp_path):
    conn = _tiny_index(
        tmp_path,
        {"p/one.py": [("one.py", "one.py", "module", 1, 3)], "p/two.py": [("two.py", "two.py", "module", 1, 3)]},
        imports=[("p/one.py", ".", "two", 1), ("p/two.py", ".", "one", 1)],
    )
    components = G.import_cycles(conn, limit=5)
    assert components and sorted(components[0]) == ["p/one.py", "p/two.py"]
    conn.close()


def test_ui_element_links_to_route_and_js(tmp_path):
    conn = _tiny_index(
        tmp_path,
        {"t.html": [], "static/app.js": [("pickMic", "pickMic", "function", 1, 5)],
         "r.py": [("r.py", "r.py", "module", 1, 5), ("endpoint", "endpoint", "function", 2, 4)]},
        routes=[("POST", "/api/x", "endpoint", "r.py", 2, "", "", "")],
    )
    conn.execute("INSERT INTO ui_links(element_id, tag, label, path, line, js_path, js_line, js_symbol, endpoints) "
                 "VALUES ('btn-x', 'button', 'Нажать', 't.html', 4, 'static/app.js', 2, 'pickMic', 'POST /api/x')")
    conn.commit()
    graph = G.build(conn, include_manifest=False, include_history=False)
    ui = G._node_id("ui", "btn-x")
    edges = {(e["rel"], e["to"]) for e in graph["edges"] if e["from"] == ui}
    assert ("handled_by", G._symbol_id("static/app.js", "pickMic")) in edges
    assert ("calls_api", G._node_id("route", "POST /api/x")) in edges
    conn.close()
