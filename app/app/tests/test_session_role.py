from __future__ import annotations

from app.app.tests.auth_helpers import access_token


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_shared_account_roles_do_not_overwrite_each_other(client, tmp_path, monkeypatch):
    from app.app.config import get_settings

    roles = tmp_path / "Login_roles.txt"
    roles.write_text("Manager | admin,manager\n", encoding="utf-8")
    monkeypatch.setenv("LOGIN_ROLES_PATH", str(roles))
    get_settings.cache_clear()

    admin = access_token(client, name="Manager", telegram_id="tg-manager", role="admin")
    manager = access_token(client, name="Manager", telegram_id="tg-manager", role="manager")
    assert client.get("/admin/conflicts", headers=_h(admin)).status_code != 403
    assert client.get("/admin/conflicts", headers=_h(manager)).status_code == 403
    again = access_token(client, name="Manager", telegram_id="tg-manager", role="admin")
    assert client.get("/admin/conflicts", headers=_h(again)).status_code != 403
    assert client.get("/admin/conflicts", headers=_h(manager)).status_code == 403
