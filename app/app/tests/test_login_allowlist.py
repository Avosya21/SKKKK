from __future__ import annotations

from pathlib import Path

from app.app.services.login_allowlist import load_allowlist, resolve_allowed_login


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
    r = anon_client.post(
        "/auth/login",
        json={"name": "stranger", "telegram_id": "tg-x", "password": "admin-pass"},
    )
    assert r.status_code == 401
    assert "списке допущенных" in r.json()["detail"]


def test_login_accepts_allowlisted_name_any_case(anon_client):
    r = anon_client.post(
        "/auth/login",
        json={"name": "ADMIN", "telegram_id": "tg-case", "password": "admin-pass"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Admin"
    assert r.json()["role"] == "admin"


def test_login_picks_up_allowlist_edit(anon_client):
    from app.app.config import get_settings

    path = Path(get_settings().login_allowlist_path)
    r = anon_client.post(
        "/auth/login",
        json={"name": "newbie@example.com", "telegram_id": "tg-new", "password": "admin-pass"},
    )
    assert r.status_code == 401

    text = path.read_text(encoding="utf-8")
    path.write_text(text + "newbie@example.com\n", encoding="utf-8")
    r2 = anon_client.post(
        "/auth/login",
        json={"name": "Newbie@Example.COM", "telegram_id": "tg-new", "password": "admin-pass"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["name"] == "newbie@example.com"
