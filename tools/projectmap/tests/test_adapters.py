"""Адаптеры стека на небольшом проекте во временной папке: роуты, таблицы, окружение, Docker, документы, запуски скриптов, экран → API."""

from __future__ import annotations

from pathlib import Path

import pytest

from projectmap import grammars
from projectmap.adapter_web import normalize_url, _argument, _url_of
from projectmap.build import build_graph, file_id, symbol_id
from projectmap.config import Config, parse_config
from projectmap.graphbuf import env_id, route_id, service_id, table_id

ROOT = Path(__file__).resolve().parents[3]

FILES = {
    "app/models.py": ('from sqlalchemy import Column, Integer, String\nfrom sqlalchemy.orm import DeclarativeBase\n\n\nclass Base(DeclarativeBase):\n    pass\n\n\n'
                      'class User(Base):\n    __tablename__ = "users"\n    id = Column(Integer, primary_key=True)\n    name = Column(String)\n\n\n'
                      'class Audit(Base):\n    __tablename__ = "audit"\n    id = Column(Integer, primary_key=True)\n'),
    "app/routes.py": ('import os\n\nfrom fastapi import APIRouter, Depends\nfrom starlette.templating import Jinja2Templates\n\nfrom app.models import Audit, User\n\n'
                      'router = APIRouter(prefix="/api")\ntemplates = Jinja2Templates(directory="templates")\n\n\n'
                      'def require_admin():\n    return True\n\n\n'
                      '@router.get("/users/{user_id}")\ndef get_user(user_id: int, admin=Depends(require_admin)):\n    """Пользователь по номеру."""\n'
                      '    return db.query(User).first()\n\n\n'
                      '@router.post("/users")\ndef create_user():\n    db.add(User(name="x"))\n    db.commit()\n    db.execute("select * from audit where 1=1")\n'
                      '    return os.environ.get("TOKEN_TTL", "5")\n\n\n'
                      '@router.get("/page")\ndef page(request):\n    return templates.TemplateResponse("page.html", {"request": request})\n'),
    "app/main.py": 'from fastapi import FastAPI\n\nfrom app import routes\n\napp = FastAPI()\napp.include_router(routes.router, prefix="/v1")\n',
    "app/config.py": 'from pydantic_settings import BaseSettings\n\n\nclass Settings(BaseSettings):\n    database_url: str = ""\n    debug: bool = False\n',
    "templates/page.html": '<button id="go" onclick="load(1)">Загрузить</button>\n',
    "web/app.js": ("function load(id) { return fetch('/v1/api/users/' + id); }\n"
                   "function save() { return fetch(`/v1/api/users`, { method: 'POST' }); }\n"
                   "function other() { return fetch('/nowhere'); }\n"),
    ".env.example": "TOKEN_TTL=5\nUNUSED_VAR=1\n",
    "docker-compose.yml": "services:\n  web:\n    build: .\n    ports:\n      - '8000:8000'\n    environment:\n      - TOKEN_TTL=1\n    command: python app/main.py\n",
    "Dockerfile": "FROM python:3.10\n",
    "docs/readme.md": "Роуты в `app/routes.py`, модели — [тут](../app/models.py), а `app/missing.py` не существует.\n",
    "scripts/deploy_tool.py": "import subprocess\nsubprocess.run(['python', 'run_migrations.py'])\n",
    "scripts/run_migrations.py": "print('ok')\n",
}


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    try:
        status = grammars.ensure(ROOT, ["javascript"])
    except grammars.GrammarUnavailable as exc:
        pytest.skip(str(exc))
    if "javascript" not in status.ready:
        pytest.skip("нет грамматики javascript")
    root = tmp_path_factory.mktemp("stack")
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    patch = pytest.MonkeyPatch()
    patch.setenv(grammars.CACHE_ENV, str(grammars.configure(ROOT)))
    try:
        yield build_graph(root, Config())
    finally:
        patch.undo()


def _edges(graph, rel):
    return {(e["from"], e["to"]): e for e in graph["edges"] if e["rel"] == rel}


def _nodes(graph, kind):
    return {n["label"]: n for n in graph["nodes"] if n["kind"] == kind}


def test_all_adapters_whose_stack_is_present_run(graph):
    ran = [n for n in graph["notes"] if n.startswith("адаптеры:")][0]
    for name in ("fastapi", "sqlalchemy", "env", "docker", "markdown", "scripts", "web"):
        assert name in ran


def test_routes_get_prefixes_from_router_and_include_router(graph):
    routes = _nodes(graph, "route")
    assert set(routes) == {"GET /v1/api/users/{user_id}", "POST /v1/api/users", "GET /v1/api/page"}
    assert routes["GET /v1/api/users/{user_id}"]["doc"] == "Пользователь по номеру." and routes["POST /v1/api/users"]["handler"] == "create_user"


def test_route_links_to_handler_dependency_and_guard(graph):
    route = route_id("GET", "/v1/api/users/{user_id}")
    handler, guard = symbol_id("app/routes.py", "get_user"), symbol_id("app/routes.py", "require_admin")
    assert (route, handler) in _edges(graph, "handles") and (route, guard) in _edges(graph, "depends_on") and (route, guard) in _edges(graph, "guarded_by")
    assert _nodes(graph, "route")["GET /v1/api/users/{user_id}"]["guards"] == "require_admin"


def test_tables_columns_and_who_reads_or_writes(graph):
    tables = _nodes(graph, "table")
    assert set(tables) == {"users", "audit"} and tables["users"]["columns"] == "id:Integer, name:String"
    assert (symbol_id("app/models.py", "User"), table_id("users")) in _edges(graph, "declares")
    assert (symbol_id("app/routes.py", "get_user"), table_id("users")) in _edges(graph, "reads")
    assert (symbol_id("app/routes.py", "create_user"), table_id("users")) in _edges(graph, "writes"), "User(...) + commit — запись"
    assert (symbol_id("app/routes.py", "create_user"), table_id("audit")) in _edges(graph, "reads"), "таблица названа в SQL select"


def test_environment_variables_from_code_settings_and_samples(graph):
    env = _nodes(graph, "env")
    assert {"TOKEN_TTL", "UNUSED_VAR", "DATABASE_URL", "DEBUG"} <= set(env)
    assert (symbol_id("app/routes.py", "create_user"), env_id("TOKEN_TTL")) in _edges(graph, "reads_env")
    assert (symbol_id("app/config.py", "Settings"), env_id("DATABASE_URL")) in _edges(graph, "reads_env")
    declared = _edges(graph, "declares")
    assert (file_id(".env.example"), env_id("UNUSED_VAR")) in declared and env["UNUSED_VAR"].get("declared") is True


def test_compose_services_with_build_command_and_environment(graph):
    service = service_id("docker-compose.yml", "web")
    assert "web" in _nodes(graph, "service") and (file_id("docker-compose.yml"), service) in _edges(graph, "declares")
    assert (service, file_id("Dockerfile")) in _edges(graph, "runs") and (service, file_id("app/main.py")) in _edges(graph, "runs")
    assert (service, env_id("TOKEN_TTL")) in _edges(graph, "declares") and _nodes(graph, "service")["web"]["ports"] == "8000:8000"


def test_documents_refer_to_existing_files_and_count_broken_links(graph):
    refers = _edges(graph, "refers_to")
    assert (file_id("docs/readme.md"), file_id("app/routes.py")) in refers and (file_id("docs/readme.md"), file_id("app/models.py")) in refers
    assert not any(t.endswith("missing.py") for _, t in refers)
    assert any(n.startswith("документы:") and "битых 1" in n for n in graph["notes"])


def test_scripts_that_name_other_scripts_are_linked_by_runs(graph):
    assert (file_id("scripts/deploy_tool.py"), file_id("scripts/run_migrations.py")) in _edges(graph, "runs")


def test_screen_calls_api_by_literal_template_and_concatenation_with_method_hints(graph):
    calls = _edges(graph, "calls_api")
    get_route, post_route = route_id("GET", "/v1/api/users/{user_id}"), route_id("POST", "/v1/api/users")
    assert (symbol_id("web/app.js", "load"), get_route) in calls, "конкатенация '/v1/api/users/' + id"
    assert (symbol_id("web/app.js", "save"), post_route) in calls and (symbol_id("web/app.js", "save"), route_id("GET", "/v1/api/users")) not in calls
    assert not [e for e in calls if e[0].endswith("::other")], "адреса, которых нет среди роутов, связью не становятся"


def test_server_code_renders_a_template_file(graph):
    assert (symbol_id("app/routes.py", "page"), file_id("templates/page.html")) in _edges(graph, "renders")


def test_adapters_can_be_chosen_in_config_and_empty_means_auto(tmp_path):
    (tmp_path / "a.md").write_text("см. `b/c.py`\n", encoding="utf-8")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "c.py").write_text("x = 1\n", encoding="utf-8")
    only_scripts = build_graph(tmp_path, parse_config({"adapters": ["scripts"]}))
    auto = build_graph(tmp_path, parse_config({"adapters": "auto"}))
    assert not [e for e in only_scripts["edges"] if e["rel"] == "refers_to"]
    assert [e for e in auto["edges"] if e["rel"] == "refers_to"]


def test_url_helpers_normalise_templates_and_extract_arguments():
    assert normalize_url("/p/{pid}/x?y=1") == "/p/{}/x" and normalize_url("/p/${id}/x") == "/p/{}/x" and normalize_url("/p/18/x/") == "/p/{}/x"
    assert _url_of("'/p/' + id + '/x'") == "/p/{}/x" and normalize_url(_url_of("`/p/${id}`")) == "/p/{}" and _url_of("base + '/x'") is None
    text = "api('/a/' + id, {method: 'POST'}) other"
    assert _argument(text, text.index("(") + 1) == "'/a/' + id"


def test_tests_that_call_routes_through_a_client_cover_the_handler(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "routes.py").write_text(
        'from fastapi import APIRouter\nrouter = APIRouter()\n\n\n@router.get("/items/{item_id}")\ndef read_item(item_id: int):\n    return 1\n\n\n'
        '@router.post("/items")\ndef add_item():\n    return 2\n', encoding="utf-8")
    (tmp_path / "tests" / "test_routes.py").write_text(
        'def test_read(client):\n    client.get("/items/5")\n\n\ndef test_add(client):\n    client.post("/items")\n    client.delete("/items")\n', encoding="utf-8")
    graph = build_graph(tmp_path, Config())
    covers = {(e["from"], e["to"]) for e in graph["edges"] if e["rel"] == "covers"}
    assert (symbol_id("tests/test_routes.py", "test_read"), symbol_id("app/routes.py", "read_item")) in covers
    assert (symbol_id("tests/test_routes.py", "test_add"), symbol_id("app/routes.py", "add_item")) in covers
    assert len([c for c in covers if c[0].endswith("test_add")]) == 1, "DELETE /items роута нет — покрытия нет"


def test_system_and_configured_environment_variables_are_ignored_by_the_sample_check():
    config = parse_config({"ignore_env": ["SMOKE_*", "PRIMARY_ANALITICS_INTERNAL"]})
    for name in ("PATH", "Path", "LocalAppData", "ProgramFiles(x86)", "SMOKE_BASE", "primary_analitics_internal"):
        assert config.is_env_ignored(name), name
    assert not config.is_env_ignored("DATABASE_URL")
