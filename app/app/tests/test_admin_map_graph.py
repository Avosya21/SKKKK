"""Страница «Граф зависимостей» (/admin/map/graph): права доступа, отдача страницы и фильтры данных.

Граф подменяется целиком (`load_graph` в роутере), а счётчики устаревания — заглушкой, поэтому тестам
не нужны ни собранный индекс карты, ни боевая БД: `data/project_map.db` и `data/app.db` не читаются.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.app.routers import admin_map_graph as amg
from app.app.services.project_index import graph as G

SYMBOL_ID = "sym:app/app/services/mod_0.py::run"
ROUTE_ID = "route:POST /chat"
TABLE_ID = "table:jobs"
ENV_ID = "env:WEB_PORT"
UI_ID = "ui:#meeting-mic"


def _file_id(index: int) -> str:
    return G._file_id(f"app/app/services/mod_{index}.py")


def _graph(files: int = 6) -> dict[str, Any]:
    """Небольшой граф: файлы, функция, роут, таблица, переменная окружения и элемент интерфейса."""
    nodes: list[dict[str, Any]] = [
        {"id": SYMBOL_ID, "kind": "symbol", "label": "run", "path": "app/app/services/mod_0.py", "line": 12,
         "group": "сервисы", "sub": "функция", "sig": "def run(project_id: int) -> None"},
        {"id": ROUTE_ID, "kind": "route", "label": "POST /chat", "path": "app/app/routers/knowledge.py", "line": 629,
         "group": "роутеры", "handler": "chat_endpoint", "guards": ["require_project"]},
        {"id": TABLE_ID, "kind": "table", "label": "jobs", "path": "app/app/models/job.py", "line": 5,
         "group": "модели БД", "cols": ["id", "status"], "writers": ["run"], "readers": ["claim"]},
        {"id": ENV_ID, "kind": "env", "label": "WEB_PORT", "path": "app/app/config.py", "line": 40,
         "group": "ядро API", "default": "1777"},
        {"id": UI_ID, "kind": "ui", "label": "Микрофон", "path": "app/app/templates/meeting_room.html", "line": 20,
         "group": "шаблоны", "endpoints": ["POST /projects/1/meetings/1/join"]},
    ]
    for index in range(files):
        path = f"app/app/services/mod_{index}.py"
        nodes.append({"id": G._file_id(path), "kind": "file", "label": f"mod_{index}.py", "path": path, "line": 1,
                      "group": "сервисы" if index % 2 == 0 else "тесты", "sub": "модуль"})
    edges: list[dict[str, Any]] = [
        {"from": SYMBOL_ID, "to": TABLE_ID, "rel": "writes", "line": 20, "n": 2},
        {"from": ROUTE_ID, "to": SYMBOL_ID, "rel": "calls", "line": 640, "n": 1},
        {"from": SYMBOL_ID, "to": ENV_ID, "rel": "reads_env", "line": 15, "n": 1},
    ]
    for index in range(max(0, files - 1)):
        edges.append({"from": _file_id(index), "to": _file_id(index + 1), "rel": "imports", "line": 3, "n": 1})
    return {
        "nodes": nodes,
        "edges": edges,
        "stats": G.stats({"nodes": nodes, "edges": edges}),
        "source": {"db": "data/project_map.db", "manifest": "docs/project-map/manifest.yaml",
                   "indexed_at": 1790802331.8, "git_head": "e8d17a9a1b2c", "chunker_version": "6"},
        "built_at": 1790802400.0,
        "notes": [],
    }


@pytest.fixture()
def graph_api(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Страница и ручки работают на подменённом графе; индекс карты не открывается."""
    graph = _graph()

    monkeypatch.setattr(amg, "load_graph", lambda **kwargs: graph)
    monkeypatch.setattr(amg, "_stale_counts",
                        lambda: {"changed": 0, "removed": 0, "no_vector": 0, "age_hours": 2.0})
    return graph


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_anonymous_gets_login_redirect_and_401(client, graph_api):
    """Без входа: HTML-оболочка — 302 на /login, данные — 401 (не JSON-страница)."""
    page = client.get("/admin/map/graph", follow_redirects=False)
    assert page.status_code == 302
    assert page.headers["location"] == "/login"
    assert client.get("/admin/map/graph/data").status_code == 401
    assert client.get("/admin/map/graph/summary").status_code == 401


def test_non_admin_is_forbidden(client, graph_api, manager_token, user_token):
    """manager и user не видят ни страницу, ни данные графа."""
    for token in (manager_token, user_token):
        headers = _headers(token)
        assert client.get("/admin/map/graph", headers=headers).status_code == 403
        assert client.get("/admin/map/graph/data", headers=headers).status_code == 403
        assert client.get("/admin/map/graph/summary", headers=headers).status_code == 403


def test_admin_page_renders_with_graph_assets(client, graph_api, admin_token):
    """Страница админа: заголовок, область графа, Cytoscape и её скрипт, ярлыки видов для легенды."""
    page = client.get("/admin/map/graph", headers=_headers(admin_token))
    assert page.status_code == 200
    assert "Граф зависимостей" in page.text
    assert 'id="amg-canvas"' in page.text
    assert 'id="amg-labels"' in page.text
    assert "/static/admin_map_graph.js" in page.text
    assert "cytoscape.min.js" in page.text
    # Ярлыки видов узлов и связей для легенды отдаёт сервер в блоке amg-labels (JSON).
    assert '"kinds"' in page.text and '"relations"' in page.text and '"file"' in page.text


def test_summary_returns_stats_and_source(client, graph_api, admin_token):
    """Сводка: цифры графа, источник, состояние индекса — из подменённого графа."""
    response = client.get("/admin/map/graph/summary", headers=_headers(admin_token))
    assert response.status_code == 200
    body = response.json()
    assert body["stats"]["nodes"] == len(graph_api["nodes"])
    assert body["stats"]["edges"] == len(graph_api["edges"])
    assert body["stats"]["by_kind"]["file"] == 6
    assert body["stats"]["by_relation"]["imports"] == 5
    assert body["source"]["db"] == "data/project_map.db"
    assert "stale" in body and "notes" in body


def test_data_defaults_to_module_view(client, graph_api, admin_token):
    """Без фокуса и подсистемы сервер отдаёт модульный вид: узлы — файлы, счётчики и источник на месте."""
    body = client.get("/admin/map/graph/data", headers=_headers(admin_token)).json()
    assert body["view"] == "modules"
    assert body["center"] is None
    assert len(body["nodes"]) == 6
    assert all(node["kind"] == "file" for node in body["nodes"])
    assert body["limit"] == 600
    assert body["total_nodes"] == len(graph_api["nodes"])
    assert body["source"]["db"] == "data/project_map.db"
    assert body["truncated"] is False


def test_module_view_keeps_file_to_file_edges(client, graph_api, admin_token):
    """Модульный вид должен показывать рёбра файл → файл.

    Идентификаторы узлов и рёбер модульного вида совпадают (`file:<путь>`), поэтому «Модули» приходят со связями.
    """
    body = client.get("/admin/map/graph/data?view=modules", headers=_headers(admin_token)).json()
    assert body["nodes"], "узлы модульного вида должны быть"
    assert body["edges"], "в модульном виде ожидаются рёбра файл → файл"
    assert all(edge["from"].startswith("file:") and edge["to"].startswith("file:") for edge in body["edges"])


def test_data_filters_by_group_and_kinds(client, graph_api, admin_token):
    """Подсистема и виды узлов — как их понимает сервер: подсистема даёт вид «сущности»."""
    headers = _headers(admin_token)
    by_group = client.get("/admin/map/graph/data?group=тесты", headers=headers).json()
    assert by_group["view"] == "entities"
    assert by_group["nodes"] and all(node["group"] == "тесты" for node in by_group["nodes"])

    by_kind = client.get("/admin/map/graph/data?group=сервисы&kinds=symbol", headers=headers).json()
    assert by_kind["view"] == "entities"
    assert [node["id"] for node in by_kind["nodes"]] == [SYMBOL_ID]

    modules = client.get("/admin/map/graph/data?view=modules&kinds=file", headers=headers).json()
    assert modules["view"] == "modules"
    assert modules["nodes"] and all(node["kind"] == "file" for node in modules["nodes"])

    relations = client.get("/admin/map/graph/data?group=сервисы&relations=writes", headers=headers).json()
    assert all(edge["rel"] == "writes" for edge in relations["edges"])


def test_data_focus_returns_neighbourhood(client, graph_api, admin_token):
    """Фокус по имени функции: центр найден, рядом его связи."""
    body = client.get("/admin/map/graph/data?focus=run&depth=1", headers=_headers(admin_token)).json()
    assert body["view"] == "entities"
    assert body["center"]["id"] == SYMBOL_ID
    ids = {node["id"] for node in body["nodes"]}
    assert SYMBOL_ID in ids
    assert TABLE_ID in ids and ROUTE_ID in ids
    assert body["edges"]


def test_data_limit_truncates_and_validation_rejects_out_of_range(client, monkeypatch, admin_token):
    """Предел узлов обрезает вид и помечает ответ, а выход за диапазон — 422."""
    monkeypatch.setattr(amg, "load_graph", lambda **kwargs: _graph(files=120))
    monkeypatch.setattr(amg, "_stale_counts",
                        lambda: {"changed": 0, "removed": 0, "no_vector": 0, "age_hours": 2.0})
    headers = _headers(admin_token)

    body = client.get("/admin/map/graph/data?limit=50", headers=headers).json()
    assert body["limit"] == 50
    assert len(body["nodes"]) == 50
    assert body["truncated"] is True
    assert body["total_nodes"] == 125
    assert any("не все узлы" in note for note in body["notes"])

    assert client.get("/admin/map/graph/data?limit=10", headers=headers).status_code == 422
    assert client.get("/admin/map/graph/data?limit=2000", headers=headers).status_code == 422
    assert client.get("/admin/map/graph/data?depth=5", headers=headers).status_code == 422
    assert client.get("/admin/map/graph/data?view=drop", headers=headers).status_code == 422


def test_stale_index_is_reported(client, graph_api, monkeypatch, admin_token):
    """Признак устаревания индекса доходит до страницы (бейдж «индекс отстаёт»)."""
    monkeypatch.setattr(amg, "_stale_counts",
                        lambda: {"changed": 3, "removed": 1, "no_vector": 2, "age_hours": 5.5})
    headers = _headers(admin_token)
    assert client.get("/admin/map/graph/data", headers=headers).json()["stale"]["changed"] == 3
    summary = client.get("/admin/map/graph/summary", headers=headers).json()
    assert summary["stale"]["no_vector"] == 2 and summary["stale"]["age_hours"] == 5.5


def test_missing_index_gives_hint_instead_of_error(client, monkeypatch, admin_token):
    """Граф не собран: страница отдаёт 200 и подсказку, а не падает."""
    def _missing(**kwargs):
        raise FileNotFoundError("нет выгрузки графа")

    monkeypatch.setattr(amg, "load_graph", _missing)
    monkeypatch.setattr(amg, "_stale_counts",
                        lambda: {"changed": 0, "removed": 0, "no_vector": 0, "age_hours": None})
    headers = _headers(admin_token)

    summary = client.get("/admin/map/graph/summary", headers=headers)
    assert summary.status_code == 200
    assert summary.json()["stats"]["nodes"] == 0
    assert any("индекс не построен" in note for note in summary.json()["notes"])

    data = client.get("/admin/map/graph/data", headers=headers)
    assert data.status_code == 200
    body = data.json()
    assert body["nodes"] == [] and body["edges"] == []
    assert any("индекс не построен" in note for note in body["notes"])
