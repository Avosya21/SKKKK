from __future__ import annotations

from app.app.tests.conftest import ADMIN_PASS, MANAGER_PASS


def _login(client, password: str) -> str:
    r = client.post("/auth/login", json={"name": "Manager", "telegram_id": "tg-manager", "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_shared_account_roles_do_not_overwrite_each_other(client):
    admin = _login(client, ADMIN_PASS)
    manager = _login(client, MANAGER_PASS)  # позже админа: раньше это ломало админскую сессию
    assert client.get("/admin/conflicts", headers=_h(admin)).status_code != 403
    assert client.get("/admin/conflicts", headers=_h(manager)).status_code == 403
    again = _login(client, ADMIN_PASS)
    assert client.get("/admin/conflicts", headers=_h(again)).status_code != 403
    assert client.get("/admin/conflicts", headers=_h(manager)).status_code == 403
