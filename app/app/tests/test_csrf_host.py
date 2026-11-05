from __future__ import annotations

from app.app.dependencies import SESSION_COOKIE_NAME


def test_csrf_post_with_cookie_rejects_missing_origin(cookie_auth_client):
    cookie_auth_client.headers.pop("Origin", None)
    r = cookie_auth_client.post("/auth/logout")
    assert r.status_code == 403
    assert "CSRF" in r.json()["detail"]


def test_csrf_post_with_cookie_rejects_foreign_origin(cookie_auth_client):
    r = cookie_auth_client.post("/auth/logout", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert "CSRF" in r.json()["detail"]


def test_csrf_post_with_cookie_allows_origin(cookie_auth_client):
    r = cookie_auth_client.post("/auth/logout", headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_csrf_skips_when_no_session_cookie(anon_client):
    anon_client.cookies.clear()
    anon_client.headers.pop("Origin", None)
    r = anon_client.post(
        "/auth/login",
        json={"name": "Admin", "telegram_id": "tg-csrf-login", "password": "admin-pass"},
    )
    assert r.status_code == 200
    assert SESSION_COOKIE_NAME in r.cookies


def test_csrf_skips_login_even_with_stale_cookie(cookie_auth_client):
    """Повторный вход с уже живой cookie не должен упираться в CSRF (префикс /auth/login)."""
    r = cookie_auth_client.post(
        "/auth/login",
        json={"name": "Admin", "telegram_id": "tg-csrf-relogin", "password": "admin-pass"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 200


def test_trusted_host_rejects_unknown_host(client):
    r = client.get("/health", headers={"Host": "evil.example"})
    assert r.status_code == 400
    assert "Host" in r.json()["detail"]


def test_trusted_host_allows_testserver(client):
    r = client.get("/health", headers={"Host": "testserver"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_trusted_host_allows_web_public_url_host(client, monkeypatch):
    from app.app.config import get_settings

    monkeypatch.setenv("WEB_PUBLIC_URL", "https://portal.example:777")
    get_settings.cache_clear()
    try:
        r = client.get("/health", headers={"Host": "portal.example:777"})
        assert r.status_code == 200
    finally:
        get_settings.cache_clear()


def test_csrf_allows_origin_from_web_public_url(cookie_auth_client, monkeypatch):
    from app.app.config import get_settings

    monkeypatch.setenv("WEB_PUBLIC_URL", "https://portal.example:777")
    get_settings.cache_clear()
    try:
        r = cookie_auth_client.post(
            "/auth/logout",
            headers={"Origin": "https://portal.example:777", "Host": "testserver"},
        )
        assert r.status_code == 200
    finally:
        get_settings.cache_clear()
