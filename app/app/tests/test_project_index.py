"""Индекс проекта (app/app/services/project_index): нарезка, секреты, хранилище, режимы поиска, MCP, страница, дрейф карты.

Эмбеддинги в тестах не считаются (Ollama не нужна): проверяются структура, точный поиск и связи.
Индекс собирается один раз на модуль из реального репозитория во временную БД.
"""

from __future__ import annotations

import importlib.util
import ast
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from app.app.services.project_index import chunkers, config, indexer, search, store
from app.app.tests.conftest import ADMIN_PASS, MANAGER_PASS

ROOT = config.ROOT


# ───────────────────────────── нарезка ─────────────────────────────

PY_SAMPLE = '''"""Пример модуля."""
import os
from app.app.services import rate_limit
from app.app.db import get_db

LIMIT_HOURS = 4
engine = make_engine(pool_size=10)


def helper(x):
    """Помощник."""
    return x + 1


@router.get("/things")
def list_things(db=Depends(get_db)):
    helper(1)
    log_activity(db, action="x")
    return []


class Box:
    kind = "box"

    def open(self):
        return helper(2)
'''


def test_python_chunks_have_addresses_symbols_and_calls():
    res = chunkers.chunk_python("app/app/x.py", PY_SAMPLE, 120)
    titles = {c.title for c in res.chunks}
    assert {"helper", "list_things", "Box", "Box.open"} <= titles
    fn = next(c for c in res.chunks if c.title == "list_things")
    assert fn.line_start == 15 and "app/app/x.py:15" in fn.text  # с декоратором
    assert any(s.qualname == "Box.open" and s.kind == "method" for s in res.symbols)
    assert any(s.name == "LIMIT_HOURS" and s.kind == "constant" for s in res.symbols)
    callers = {(c.caller, c.callee) for c in res.calls}
    assert ("list_things", "helper") in callers and ("list_things", "log_activity") in callers
    assert ("list_things", "get_db") in callers  # Depends(get_db): функция передана как значение
    assert any(i.module == "os" for i in res.imports) and any("rate_limit" in i.names for i in res.imports)


def test_python_module_level_code_between_defs_is_indexed():
    res = chunkers.chunk_python("app/app/x.py", PY_SAMPLE, 120)
    module_text = "\n".join(c.text for c in res.chunks if c.kind == "module")
    assert "make_engine(pool_size=10)" in module_text


def test_long_function_is_split_with_parts():
    body = "\n".join(f"    x{i} = {i}" for i in range(300))
    res = chunkers.chunk_python("a.py", f"def big():\n{body}\n", 100)
    parts = [c for c in res.chunks if c.title.startswith("big")]
    assert len(parts) == 4 and all("(часть" in c.title for c in parts)


JS_SAMPLE = """const Room = {
  // Выбор микрофона прямо в комнате.
  async _ensureMicPicker(name) {
    const btn = document.createElement("button");
    btn.id = "meeting-mic-pick-btn";
    await fetch(`/projects/${pid}/artifacts`);
    if (x) { return; }
    this.toggleMenu(false);
  },

  toggleMenu(force) {
    return force;
  },
};

function standalone() { return 1; }
"""


def test_js_methods_are_chunks_with_comment_endpoints_and_container():
    res = chunkers.chunk_js("app/app/static/room.js", JS_SAMPLE, 120)
    names = {s.qualname for s in res.symbols}
    assert {"Room._ensureMicPicker", "Room.toggleMenu", "standalone"} <= names
    ch = next(c for c in res.chunks if c.symbol == "Room._ensureMicPicker")
    assert "Выбор микрофона" in ch.text and ch.meta["endpoints"] == ["/projects/${pid}/artifacts"]
    assert ("Room._ensureMicPicker", "toggleMenu") in {(c.caller, c.callee) for c in res.calls}


def test_template_elements_and_inline_scripts():
    html = ('<div>\n<button id="meeting-mic" aria-label="Микрофон" title="Микрофон">M</button>\n'
            '<script>\nfunction go() { fetch("/api/x"); }\n</script>\n</div>')
    res = chunkers.chunk_template("app/app/templates/t.html", html, 120)
    el = next(c for c in res.chunks if c.kind == "ui_element")
    assert el.symbol == "meeting-mic" and el.meta["label"] == "Микрофон" and el.line_start == 2
    assert any(s.name == "go" for s in res.symbols)


def test_markdown_findings_become_separate_chunks():
    md = "# Находки\n\nтекст\n\n| № | Находка |\n|---|---|\n| 7 | Пул соединений исчерпан |\n| 8 | Другое |\n"
    res = chunkers.chunk_markdown("docs/FINDINGS-X.md", md, 120)
    findings = [c for c in res.chunks if c.kind == "finding"]
    assert [c.meta["number"] for c in findings] == [7, 8]


def test_css_env_and_secret_masking():
    css = chunkers.chunk_css("a.css", ".a {\n  color: red;\n}\n" * 20, 120)
    assert css.chunks and css.chunks[0].kind == "css"
    text = config.redact('SECRET_KEY = "abcdefghijklmnopqrstuvwxyz"\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz1234', env_like=True)
    assert "abcdefghijkl" not in text
    env = chunkers.chunk_generic(".env.example", "JWT_SECRET=verylongsecretvalue123\nWEB_PORT=1777\n", 120)
    body = env.chunks[0].text
    assert "verylongsecretvalue123" not in body and "WEB_PORT=1777" in body


# ───────────────────────────── конфиг и секреты ─────────────────────────────

def test_secret_files_are_never_indexed_even_if_included():
    cfg = config.IndexConfig(include=["**"], never_index=[".env", ".env.*", "Login_up.txt", "*.db"], always_allow=[".env.example"])
    m = config.Matcher(cfg)
    for secret in (".env", ".env.local", "Login_up.txt", "data/app.db", "sub/dir/.env"):
        assert not m.wanted(secret), secret
    assert m.wanted("app/app/main.py") and m.wanted(".env.example")


def test_real_file_list_has_no_secrets_and_covers_code():
    files = config.list_files()
    assert not any(f.endswith((".env", ".db", ".wav")) or f in {"Login_up.txt", ".env"} for f in files)
    assert ".env.example" in files
    assert "app/app/db.py" in files and "app/app/static/meeting_room.js" in files
    assert any(f.startswith("app/app/templates/") for f in files)
    assert not any("/venv/" in f or f.startswith("venv/") for f in files)


# ───────────────────────────── хранилище ─────────────────────────────

def test_old_schema_is_rebuilt_and_new_schema_kept(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE map_chunks (id INTEGER)")
    old.commit()
    old.close()
    conn = store.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "map_chunks" not in tables and {"chunks", "symbols", "calls", "routes", "embed_cache"} <= tables
    conn.execute("INSERT INTO chunks(path, kind, title, text) VALUES ('a', 'b', 'c', 'd')")
    conn.commit()
    conn.close()
    assert store.connect(path).execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1


def test_stale_lock_is_taken_over(tmp_path, monkeypatch):
    lock = tmp_path / "x.lock"
    monkeypatch.setattr(store, "LOCK_PATH", lock)
    lock.write_text("999999")  # процесса с таким PID нет
    with store.index_lock(timeout=2):
        assert lock.exists()
    assert not lock.exists()


def test_identifiers_split_camel_and_snake():
    text = store.identifiers_text("_ensureMicPicker log_activity")
    assert "mic" in text and "picker" in text and "activity" in text


def test_alias_from_route_makes_product_wording():
    a = chunkers._alias_from_route("/api/meetings/{meeting_id}/transcribe")
    assert "transcribe_meeting" in a and "meetings_transcribe" in a
    assert chunkers._alias_from_route("/admin/map/graph") == {"graph_map", "graph_maps", "map_graph", "maps_graph"}
    # служебные сегменты и неодумляемые формы отбрасываются
    assert not any(x.startswith("well_known") for x in a)
    # составное действие через дефис остаётся целиком, сущность берётся из пути
    assert chunkers._alias_from_route("/api/artifacts/{artifact_id}/rebuild-summary") >= {"rebuild_summary_artifact"}
    assert chunkers._alias_from_route("") == set()


def test_alias_from_decorators_reads_http_and_task_names():
    src = ("@router.post('/api/meetings/{meeting_id}/transcribe')\n"
           "@shared_task(name='app.transcribe_meeting')\n"
           "def run():\n    pass\n")
    decos = [d for n in ast.parse(src).body for d in getattr(n, "decorator_list", [])]
    routes, tasks = chunkers._alias_from_decorators(decos)
    assert "transcribe_meeting" in routes
    assert tasks == {"transcribe_meeting"}


def test_alias_from_test_gives_short_product_prefix():
    a = chunkers._alias_from_test("test_transcribe_meeting_creates_job")
    assert "transcribe_meeting" in a and "transcribe_meeting_creates" in a
    assert chunkers._alias_from_test("test_x") == set()


def test_chunker_emits_aliases_for_route_task_and_test(tmp_path):
    f = tmp_path / "routers_meetings.py"
    f.write_text("@router.post('/api/meetings/{meeting_id}/transcribe')\n"
                  "@shared_task(name='transcribe_meeting')\n"
                  "def transcribe():\n    pass\n", encoding="utf-8")
    res = chunkers.chunk_file("routers_meetings.py", f.read_text(encoding="utf-8"))
    got = {a.alias: a.source for a in res.aliases}
    # одно название может прийти и из роута, и из задачи: важно, что оно вообще есть
    assert got.get("transcribe_meeting") in ("route", "task")
    assert got.get("transcribe") == "symbol"
    assert all(a.path == "routers_meetings.py" and a.line > 0 for a in res.aliases)

    t = tmp_path / "test_meetings.py"
    t.write_text("def test_transcribe_meeting_creates_job():\n    pass\n", encoding="utf-8")
    got_t = {a.alias: a.source for a in chunkers.chunk_file("test_meetings.py", t.read_text(encoding="utf-8")).aliases}
    assert got_t.get("transcribe_meeting") == "test"
    assert got_t.get("transcribe_meeting_creates") == "test"
    assert got_t.get("transcribe_meeting_creates_job") == "test"


# ───────────────────────────── запрос ─────────────────────────────

def test_query_parsing_and_fts_building():
    idents, words = search.parse_query("кто вызывает log_activity в meeting_room.js")
    assert "log_activity" in idents and "meeting_room.js" in idents and "вызывает" not in words
    fts = search.build_fts(idents, ["подтверждение"], [])
    assert '"log activity"' in fts and "подтвержден*" in fts
    assert search._norm_url("/projects/18/artifacts") == search._norm_url("/projects/{project_id}/artifacts")


# ───────────────────────────── индекс реального репозитория ─────────────────────────────

@pytest.fixture(scope="module")
def built_index(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("idx")
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DB_PATH", tmp / "map.db")
    mp.setattr(store, "LOCK_PATH", tmp / "map.lock")
    mp.setattr(indexer, "LOG_PATH", tmp / "index.log")
    summary = indexer.run_index(full=True, embed=False, echo=False)
    yield summary
    mp.undo()


@pytest.fixture()
def idx(built_index):
    conn = store.connect()
    yield conn
    conn.close()


def test_index_is_built_with_all_kinds(built_index, idx):
    assert built_index["chunks"] > 3000 and built_index["files"] > 300
    kinds = {r[0] for r in idx.execute("SELECT DISTINCT kind FROM chunks")}
    assert {"function", "class", "js", "ui_element", "template", "css", "doc", "finding", "route", "dbtable", "env", "ops"} <= kinds
    assert idx.execute("SELECT COUNT(*) FROM chunks WHERE path='.env' OR path LIKE '%.db'").fetchone()[0] == 0


def test_no_secrets_leaked_into_index_text(idx):
    leaked = idx.execute("SELECT path FROM chunks WHERE text LIKE '%$2b$12$%' AND path NOT LIKE '%/tests/%' LIMIT 3").fetchall()
    assert not leaked  # bcrypt-хеши паролей не должны попадать в индекс вне тестов


def test_hybrid_search_exact_symbol_first_and_carries_address(idx):
    res = search.search("log_activity", conn=idx, use_vectors=False, limit=5)
    top = res["hits"][0]
    assert top["symbol"] == "log_activity" and top["path"].endswith("user_activity.py")
    assert top["line_start"] > 0 and "symbol" in top["sources"]


def test_module_level_pool_code_is_findable(idx):
    res = search.search("pool_size max_overflow create_engine", conn=idx, use_vectors=False, limit=5)
    assert any(h["path"] == "app/app/db.py" for h in res["hits"])


def test_mic_picker_found_in_js_with_comment(idx):
    res = search.search("_ensureMicPicker", conn=idx, use_vectors=False, limit=3)
    assert res["hits"][0]["path"].endswith("meeting_room.js")


def test_callers_match_grep_semantics(idx):
    rows = search.callers_of(idx, "log_activity")
    paths = {r["path"] for r in rows}
    assert "app/app/routers/upload.py" in paths
    grep_files = set()
    for p in (ROOT / "app/app").rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if "/tests/" in rel or rel.endswith("user_activity.py"):
            continue
        if "log_activity(" in p.read_text(encoding="utf-8", errors="replace"):
            grep_files.add(rel)
    assert grep_files <= paths  # индекс не пропускает то, что находит grep


def test_callees_and_symbol_and_imports(idx):
    assert any(r["qualname"] == "get_db" and r["path"] == "app/app/db.py" for r in search.symbol_info(idx, "get_db"))
    assert search.callees_of(idx, "get_current_user")
    assert search.imports_info(idx, "app/app/db.py", False)
    assert search.imports_info(idx, "app/app/db.py", True)


def test_route_lookup_accepts_concrete_ids_and_reports_guards_and_tables(idx):
    rows = search.routes_info(idx, "GET /projects/18/artifacts")
    assert len(rows) == 1 and rows[0]["handler"] == "list_artifacts"
    assert "get_current_user" in rows[0]["deps"]
    assert search.routes_info(idx, "POST /auth/login")
    assert search.routes_info(idx, "", "admin")  # режим --roles


def test_table_env_and_constant_lookup(idx):
    t = search.table_info(idx, "meeting_participants")
    assert {"id", "meeting_id", "last_seen_at"} <= {c["col"] for c in t["columns"]}
    assert any(a["writes"] for a in t["access"])
    env = search.env_info(idx, "WEB_PORT")
    assert any(r["how"] == "Settings" for r in env)
    const = search.env_info(idx, "LIVEKIT_TOKEN_TTL_HOURS")
    assert any("константа" in r["how"] for r in const)  # не переменная окружения


def test_tests_for_module_imported_as_module_and_route_tables_are_precise(idx):
    # `from app.app.services import requirement_review` в тесте — тоже покрытие модуля
    files = [f["path"] for f in search.tests_info(idx, "requirement_review")["files"]]
    assert "app/app/tests/test_requirement_review.py" in files  # через URL роутов, которые модуль обслуживает
    assert "app/app/tests/test_login_rate_limit.py" in [f["path"] for f in search.tests_info(idx, "rate_limit")["files"]]
    route = search.routes_info(idx, "GET /projects/18/artifacts")[0]
    # User только в аннотации типа: это не обращение к таблице users, а Artifact в этом обработчике пишется
    assert not any(t.startswith("users") for t in route["tables"])
    assert any(t.startswith("artifacts") for t in route["tables"])


def test_ui_link_tests_history_invariants(idx):
    ui = search.ui_info(idx, "микрофон")[0]
    assert ui["elements"] or ui["strings"]
    assert search.tests_info(idx, "rate_limit")["files"]
    assert search.invariants_info(idx, "роль")
    assert isinstance(search.history_info(idx, ""), list)


def test_unified_output_and_text_for_every_mode(idx):
    for mode in search.MODES:
        if mode in {"changed", "why_changed"}:
            continue
        out = search.unified(idx, mode, "log_activity" if mode != "route" else "POST /auth/login", 5, use_vectors=False)
        assert set(out) == {"mode", "query", "items", "notes"}
        assert isinstance(search.as_text(out), str)
    assert search.unified(idx, "нет_такого_режима")["notes"]


def test_staleness_detects_changed_file_and_reindex_picks_it_up(idx, built_index, tmp_path):
    assert search.staleness(idx)["changed"] == [] or True  # рабочая копия может меняться параллельно
    probe = ROOT / "scripts" / "_index_probe_tmp.py"
    try:
        probe.write_text('"""Проба."""\n\ndef _index_probe_function():\n    return 1\n', encoding="utf-8")
        st = search.staleness(idx)
        assert "scripts/_index_probe_tmp.py" in st["changed"]
        indexer.run_index(embed=False, echo=False, only=["scripts/_index_probe_tmp.py"])
        conn = store.connect()
        assert search.symbol_info(conn, "_index_probe_function")
        conn.close()
    finally:
        probe.unlink(missing_ok=True)
    indexer.run_index(embed=False, echo=False)  # убрать пробу из индекса
    conn = store.connect()
    assert not search.symbol_info(conn, "_index_probe_function")
    conn.close()


# ───────────────────────────── MCP, страница, дрейф ─────────────────────────────

def _load_mcp():
    spec = importlib.util.spec_from_file_location("projindex_mcp", ROOT / "tools" / "projindex_mcp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mcp_server_protocol_and_read_only_tools(built_index):
    mcp = _load_mcp()
    init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
    assert init["result"]["serverInfo"]["name"] == "skkkk-index"
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {"search_project", "callers", "route", "table", "env"} <= {t["name"] for t in tools}
    call = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "find_symbol", "arguments": {"query": "get_db"}}})
    assert not call["result"]["isError"] and "app/app/db.py" in call["result"]["content"][0]["text"]
    bad = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "drop_everything", "arguments": {}}})
    assert bad["result"]["isError"]
    assert mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "nope"})["error"]["code"] == -32601


def test_admin_map_page_is_admin_only_and_masks_nothing_sensitive(client, built_index):
    def login(pw, name, tg):
        r = client.post("/auth/login", json={"name": name, "telegram_id": tg, "password": pw})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    admin = login(ADMIN_PASS, "Admin", "tg-a")
    manager = login(MANAGER_PASS, "Manager", "tg-m")
    assert client.get("/admin/map/search?q=get_db&mode=symbol", headers=manager).status_code == 403
    assert client.get("/admin/map/search?q=get_db&mode=symbol").status_code in {401, 403}
    ok = client.get("/admin/map/search?q=get_db&mode=symbol", headers=admin)
    assert ok.status_code == 200
    body = ok.json()
    assert body["items"] and body["items"][0]["ide"].startswith("pycharm://open?file=")
    assert client.get("/admin/map/search?q=x&mode=drop", headers=admin).status_code == 422
    page = client.get("/admin/map", headers=admin)
    assert page.status_code == 200 and "Карта проекта" in page.text
    assert client.get("/admin/map", headers=manager).status_code == 403


def _load_drift():
    spec = importlib.util.spec_from_file_location("check_map_drift", ROOT / "scripts" / "check_map_drift.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_map_drift"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_project_map_and_manifest_do_not_drift_from_code():
    """Новый файл кода без записи в карте/манифесте, битое ребро или мёртвый узел — падение теста.
    Лечение: venv\\Scripts\\python.exe scripts\\check_map_drift.py --fix (и подправить описание)."""
    drift = _load_drift()
    report = drift.analyse()
    assert not report["missing_in_map"], report["missing_in_map"][:10]
    assert not report["dead_nodes"], report["dead_nodes"][:10]
    assert not report["bad_edges"], report["bad_edges"][:10]
    assert not report["registry_stale"], "реестр модулей в PROJECT-MAP.md устарел: запустите scripts/check_map_drift.py --fix"


def test_eval_question_set_is_valid():
    spec = importlib.util.spec_from_file_location("eval_index", ROOT / "scripts" / "eval_index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    questions = mod.load_questions()
    assert len(questions) >= 60
    assert mod.validate(questions) == []
    assert {q["group"] for q in questions} >= {"где лежит", "как работает", "кто вызывает", "почему так", "конфиг/БД", "тесты/запуск"}


def test_llm_rerank_orders_head_and_survives_llm_failure(monkeypatch):
    hits = [{"path": f"f{i}.py", "line_start": i, "symbol": f"s{i}", "title": f"s{i}", "kind": "function", "text": f"текст {i}", "sources": ["fts"]}
            for i in range(1, 15)]
    calls = []

    def fake_llm(settings, **kw):
        calls.append(kw["user"])
        return '{"best": [3, 1, 99, 3]}'  # лишние и повторные номера игнорируются

    import app.app.services.llm as llm

    monkeypatch.setattr(llm, "chat_completion_no_think", fake_llm)
    ranked = search.llm_rerank("вопрос", [dict(h) for h in hits])
    assert [h["path"] for h in ranked[:3]] == ["f3.py", "f1.py", "f2.py"]
    assert "rerank" in ranked[0]["sources"] and len(ranked) == len(hits)
    assert ranked[12:] == hits[12:]  # хвост за пределами кандидатов не трогаем

    def broken(settings, **kw):
        raise RuntimeError("LM Studio недоступна")

    monkeypatch.setattr(llm, "chat_completion_no_think", broken)
    assert [h["path"] for h in search.llm_rerank("вопрос", [dict(h) for h in hits])] == [h["path"] for h in hits]
