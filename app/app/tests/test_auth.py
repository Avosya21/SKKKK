from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import jwt


def test_login_creates_user_with_admin_role(client, admin_token):
    r = client.get("/projects", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200


def test_login_sets_session_cookie(client, admin_token):
    from app.app.dependencies import SESSION_COOKIE_NAME

    assert client.cookies.get(SESSION_COOKIE_NAME)


def test_login_existing_user_wrong_password_401(client, admin_token):
    r = client.post(
        "/auth/login",
        json={"name": "Admin", "telegram_id": "tg-admin", "password": "wrong"},
    )
    assert r.status_code == 401


def test_login_existing_user_takes_role_from_password(client, user_token):
    r = client.post(
        "/auth/login",
        json={"name": "User", "telegram_id": "tg-user", "password": "admin-pass"},
    )
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_token_contains_role_and_expiry(client, admin_token):
    from app.app.config import get_settings

    settings = get_settings()
    payload = jwt.decode(admin_token, settings.secret_key, algorithms=["HS256"])
    assert payload["role"] == "admin"
    assert "exp" in payload
    exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    delta = exp - datetime.now(timezone.utc)
    expected = timedelta(minutes=settings.jwt_expire_minutes)
    assert abs(delta - expected) < timedelta(minutes=2)
    assert settings.jwt_expire_minutes == 43_200


def test_expired_token_rejected(client):
    from app.app.config import get_settings

    settings = get_settings()
    exp = datetime.now(timezone.utc) - timedelta(minutes=1)
    token = jwt.encode({"sub": "1", "role": "admin", "exp": exp}, settings.secret_key, algorithm="HS256")
    r = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_tampered_token_rejected(client, admin_token):
    bad = admin_token[:-4] + "xxxx"
    r = client.get("/projects", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401
