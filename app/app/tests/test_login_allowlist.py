from __future__ import annotations

from pathlib import Path

from app.app.services.login_allowlist import load_allowlist, resolve_allowed_login
from app.app.tests.auth_helpers import login_via_code


def test_allowlist_skips_blank_and_comments(tmp_path: Path):
    path = tmp_path / "Login_up.txt"
    path.write_text("# header\n\nAnna\n  demo_user  \n", encoding="utf-8")
    entries = load_allowlist(path)
    assert set(entries) == {"anna", "demo_user"}
    assert entries["anna"] == "Anna"


def test_resolve_allowed_login_is_casefold(tmp_path: Path):
    path = tmp_path / "Login_up.txt"
    path.write_text("user1@example.com\ndemo_user\n", encoding="utf-8")
    assert resolve_allowed_login("DEMO_USER", path) == "demo_user"
    assert resolve_allowed_login("User1@Example.COM", path) == "user1@example.com"
    assert resolve_allowed_login("nobody", path) is None


def test_missing_allowlist_denies(tmp_path: Path):
    assert resolve_allowed_login("Anna", tmp_path / "missing.txt") is None


def test_login_rejects_name_not_on_list(anon_client):
    r = anon_client.post("/auth/login", json={"name": "stranger", "telegram_id": "tg-x"})
    assert r.status_code == 401
    from app.app.routers.auth import LOGIN_BAD_PAIR

    assert r.json()["detail"] == LOGIN_BAD_PAIR


def test_login_accepts_allowlisted_name_any_case(anon_client):
    data = login_via_code(anon_client, name="ADMIN", telegram_id="tg-case")
    assert data["name"] == "Admin"
    assert data["role"] == "admin"


def test_login_picks_up_allowlist_edit(anon_client, tmp_path, monkeypatch):
    from app.app.config import get_settings

    path = Path(get_settings().login_allowlist_path)
    r = anon_client.post("/auth/login", json={"name": "newbie@example.com", "telegram_id": "tg-new"})
    assert r.status_code == 401

    text = path.read_text(encoding="utf-8")
    path.write_text(text + "newbie@example.com\n", encoding="utf-8")
    roles = Path(get_settings().login_roles_path)
    roles.write_text(roles.read_text(encoding="utf-8") + "newbie@example.com | manager\n", encoding="utf-8")
    data = login_via_code(anon_client, name="Newbie@Example.COM", telegram_id="tg-new")
    assert data["name"] == "newbie@example.com"
