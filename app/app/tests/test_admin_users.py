"""Панель пользователей /admin/users: доступ по списку Panel_users.txt, таблица учёток, лента действий."""
from __future__ import annotations

import pytest


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def panel_file(tmp_path, monkeypatch):
    from app.app.config import get_settings

    path = tmp_path / "Panel_users.txt"
    monkeypatch.setenv("PANEL_USERS_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def test_no_file_means_no_access_even_for_admin(client, admin_token, panel_file):
    assert not panel_file.exists()
    for path in ("/admin/users/data", "/admin/users/activity"):
        assert client.get(path, headers=_auth(admin_token)).status_code == 404
    assert client.get("/admin/users", headers=_auth(admin_token)).status_code == 404


def test_only_listed_admin_gets_in(client, admin_token, manager_token, user_token, panel_file):
    panel_file.write_text("# комментарий\n\n  ADMIN  \n", encoding="utf-8")  # регистр и пробелы не важны
    assert client.get("/admin/users/data", headers=_auth(admin_token)).status_code == 200
    assert client.get("/admin/users", headers=_auth(admin_token)).status_code == 200
    for token in (manager_token, user_token):
        assert client.get("/admin/users/data", headers=_auth(token)).status_code == 404
        assert client.get("/admin/users", headers=_auth(token)).status_code == 404
    client.cookies.clear()  # вход выполнен только по заголовку: без него и без cookie — не авторизован
    assert client.get("/admin/users/data").status_code in (401, 403)


def test_listed_login_must_still_be_admin(client, manager_token, panel_file):
    panel_file.write_text("Manager\n", encoding="utf-8")  # в списке, но роль — менеджер
    assert client.get("/admin/users/data", headers=_auth(manager_token)).status_code == 404


def test_file_is_read_on_every_request(client, admin_token, panel_file):
    panel_file.write_text("Admin\n", encoding="utf-8")
    assert client.get("/admin/users/data", headers=_auth(admin_token)).status_code == 200
    panel_file.write_text("кто-то другой\n", encoding="utf-8")  # логин убрали — доступ пропал без перезапуска
    assert client.get("/admin/users/data", headers=_auth(admin_token)).status_code == 404


def test_users_table_has_cards_activity_and_no_secrets(client, admin_token, manager_token, panel_file):
    panel_file.write_text("Admin\n", encoding="utf-8")
    client.post("/projects", json={"name": "Для журнала"}, headers=_auth(manager_token))
    data = client.get("/admin/users/data", headers=_auth(admin_token)).json()
    assert data["summary"]["users"] >= 2
    by_login = {u["login"]: u for u in data["users"]}
    assert {"Admin", "Manager"} <= set(by_login)
    manager = by_login["Manager"]
    assert manager["role"] == "manager" and manager["logins"] >= 1 and manager["last_login"] and manager["actions"] >= 2
    assert manager["projects_owned"] == 1 and manager["registered_at"].endswith("+00:00")
    text = str(data)
    assert "password" not in text.lower() and "telegram" not in text.lower()  # хеши и Telegram ID наружу не отдаются
    assert any(p["name"] == "Для журнала" for p in data["filters"]["projects"])
    assert {"auth", "project"} <= {b["id"] for b in data["filters"]["blocks"]}


def test_activity_feed_filters_and_paging(client, admin_token, manager_token, panel_file):
    panel_file.write_text("Admin\n", encoding="utf-8")
    h = _auth(admin_token)
    for i in range(3):
        client.post("/projects", json={"name": f"Проект журнала {i}"}, headers=_auth(manager_token))
    feed = client.get("/admin/users/activity", headers=h).json()
    assert feed["items"] and feed["items"][0]["id"] > feed["items"][-1]["id"]  # новые сверху
    manager_id = next(u["id"] for u in client.get("/admin/users/data", headers=h).json()["users"] if u["login"] == "Manager")
    only = client.get(f"/admin/users/activity?user_id={manager_id}&block=project&action=create", headers=h).json()
    assert len(only["items"]) == 3 and all(e["user"] == "Manager" and e["project"].startswith("Проект журнала") for e in only["items"])
    page1 = client.get(f"/admin/users/activity?user_id={manager_id}&limit=2", headers=h).json()
    assert len(page1["items"]) == 2 and page1["more"] and page1["next_before_id"] == page1["items"][-1]["id"]
    page2 = client.get(f"/admin/users/activity?user_id={manager_id}&limit=2&before_id={page1['next_before_id']}", headers=h).json()
    assert {e["id"] for e in page1["items"]}.isdisjoint({e["id"] for e in page2["items"]})
    found = client.get("/admin/users/activity?q=Проект журнала 1", headers=h).json()["items"]
    assert found and all("Проект журнала 1" in (e["title"] + e["content"]) for e in found)
    assert client.get("/admin/users/activity?date_from=2999-01-01", headers=h).json()["items"] == []
    assert client.get("/admin/users/activity?date_from=не-дата&block=нет_такого", headers=h).json()["items"] == []
    assert client.get("/admin/users/activity?limit=100000", headers=h).status_code == 422  # предел размера страницы


def test_page_markup_and_static_files(client, admin_token, panel_file):
    panel_file.write_text("Admin\n", encoding="utf-8")
    html = client.get("/admin/users", headers=_auth(admin_token)).text
    assert "Кто зарегистрирован" in html and "Что делали" in html and "/admin/users/activity" in html
