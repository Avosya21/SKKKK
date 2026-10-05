"""Страница «Обзор карты» (/admin/map/overview): права, оболочка с фреймом (тот же источник: X-Frame-Options SAMEORIGIN его допускает), самостоятельная страница просмотрщика и пересборка графа.

Мост к инструменту подменяется (роутер) или направляется на маленький проект во временной папке (сам мост), поэтому тестам не нужен
ни разбор всего репозитория, ни боевая БД.
"""

from __future__ import annotations

import pytest

from app.app.routers import admin_map_graph as amg
from app.app.services import projectmap_view


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def fake_bridge(monkeypatch):
    calls = {"rebuild": 0}
    monkeypatch.setattr(amg.projectmap_view, "page_html", lambda: "<!doctype html><title>Проба</title><p>viewer</p>")
    monkeypatch.setattr(amg.projectmap_view, "state", lambda: {"exists": True, "built_at": 1790802400.0})

    def rebuild():
        calls["rebuild"] += 1
        return {"built_at": 1790802500.0, "nodes": 10, "edges": 20, "notes": []}

    monkeypatch.setattr(amg.projectmap_view, "rebuild", rebuild)
    return calls


def test_anonymous_gets_login_redirect_and_401(client, fake_bridge):
    assert client.get("/admin/map/overview", follow_redirects=False).status_code == 302
    assert client.get("/admin/map/overview/frame", follow_redirects=False).status_code == 302
    assert client.get("/admin/map/overview/state").status_code == 401
    assert client.post("/admin/map/overview/rebuild").status_code == 401
    assert fake_bridge["rebuild"] == 0


def test_non_admin_is_forbidden_everywhere(client, fake_bridge, manager_token, user_token):
    for token in (manager_token, user_token):
        headers = _headers(token)
        assert client.get("/admin/map/overview", headers=headers).status_code == 403
        assert client.get("/admin/map/overview/frame", headers=headers).status_code == 403
        assert client.get("/admin/map/overview/state", headers=headers).status_code == 403
        assert client.post("/admin/map/overview/rebuild", headers=headers).status_code == 403
    assert fake_bridge["rebuild"] == 0


def test_admin_gets_shell_with_frame_and_standalone_viewer(client, fake_bridge, admin_token):
    headers = _headers(admin_token)
    shell = client.get("/admin/map/overview", headers=headers)
    assert shell.status_code == 200 and "Обзор карты" in shell.text
    assert 'src="/admin/map/overview/frame"' in shell.text
    frame = client.get("/admin/map/overview/frame", headers=headers)
    assert frame.status_code == 200 and "viewer" in frame.text and frame.headers["cache-control"] == "no-store"
    assert client.get("/admin/map/overview/state", headers=headers).json() == {"exists": True, "built_at": 1790802400.0}
    rebuilt = client.post("/admin/map/overview/rebuild", headers=headers)
    assert rebuilt.status_code == 200 and rebuilt.json()["nodes"] == 10 and fake_bridge["rebuild"] == 1


def test_old_pages_link_to_the_overview(client, admin_token):
    for url in ("/admin/map", "/admin/map/graph"):
        assert "/admin/map/overview" in client.get(url, headers=_headers(admin_token)).text


@pytest.fixture()
def small_project(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text('"""Модуль а."""\nfrom pkg import b\n\n\ndef run():\n    return b.work()\n', encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("def work():\n    return 1\n", encoding="utf-8")
    (tmp_path / ".projectmap").mkdir()
    (tmp_path / ".projectmap" / "config.yaml").write_text("title: Проба\nide: 'ide://open?file={root}/{path}&line={line}'\ngraph: .projectmap/out/graph.json\n", encoding="utf-8")
    monkeypatch.setattr(projectmap_view, "ROOT", tmp_path)
    monkeypatch.setitem(projectmap_view._CACHE, "stamp", None)
    return tmp_path


def test_bridge_builds_the_graph_on_first_page_and_caches_by_file_time(small_project):
    assert projectmap_view.state() == {"exists": False, "built_at": None}
    html = projectmap_view.page_html()
    assert "<title>Проба</title>" in html and "pkg/a.py" in html and "ide://open?file=" in html
    assert projectmap_view.state()["exists"] is True
    assert projectmap_view.page_html() is projectmap_view._CACHE["html"], "второй показ берёт страницу из кэша"


def test_bridge_rebuild_refreshes_graph_and_drops_the_cache(small_project):
    projectmap_view.page_html()
    (small_project / "pkg" / "c.py").write_text("def more():\n    return 2\n", encoding="utf-8")
    result = projectmap_view.rebuild()
    assert result["nodes"] and result["edges"] is not None and projectmap_view._CACHE["stamp"] is None
    assert "pkg/c.py" in projectmap_view.page_html()
