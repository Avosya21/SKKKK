from __future__ import annotations

from app.app.dependencies import SESSION_COOKIE_NAME


def test_login_sets_httponly_session_cookie(anon_client):
    r = anon_client.post(
        "/auth/login",
        json={"name": "Admin", "telegram_id": "tg-cookie", "password": "admin-pass"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    cookie = r.cookies.get(SESSION_COOKIE_NAME)
    assert cookie
    assert cookie == body["access_token"]
    header = r.headers.get("set-cookie", "")
    assert "HttpOnly" in header or "httponly" in header.lower()
    assert "Secure" not in header  # TestClient is http
    assert "samesite=lax" in header.lower()
    from app.app.config import get_settings

    max_age = int(get_settings().jwt_expire_minutes) * 60
    assert f"Max-Age={max_age}" in header or f"max-age={max_age}" in header.lower()


def test_projects_ok_with_cookie_and_no_bearer(cookie_auth_client):
    r = cookie_auth_client.get("/projects")
    assert r.status_code == 200, r.text


def test_projects_ok_with_bearer_without_cookie(client, admin_token):
    client.cookies.clear()
    r = client.get("/projects", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200


def test_tampered_bearer_rejected_even_if_cookie_valid(cookie_auth_client, admin_token):
    bad = admin_token[:-4] + "xxxx"
    r = cookie_auth_client.get("/projects", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


def test_logout_clears_cookie(cookie_auth_client):
    assert cookie_auth_client.get("/projects").status_code == 200
    r = cookie_auth_client.post("/auth/logout")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert cookie_auth_client.get("/projects").status_code == 401
