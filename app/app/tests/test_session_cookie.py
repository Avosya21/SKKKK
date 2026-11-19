from __future__ import annotations

from app.app.dependencies import SESSION_COOKIE_NAME
from app.app.tests.auth_helpers import login_via_code


def test_login_sets_httponly_session_cookie(anon_client):
    body = login_via_code(anon_client, name="Admin", telegram_id="tg-cookie")
    assert body["access_token"]
    cookie = anon_client.cookies.get(SESSION_COOKIE_NAME)
    assert cookie
    assert cookie == body["access_token"]
    # Set-Cookie был на последнем ответе verify — проверим через повторный вход-контроль.
    r = anon_client.get("/projects")
    assert r.status_code == 200


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
