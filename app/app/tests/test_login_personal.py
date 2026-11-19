"""Вход: пара логин+пароль; регистрация: логин → код → setup (PERSONAL_PASSWORD=required)."""

from __future__ import annotations

import pytest

from app.app.routers.auth import LOGIN_BAD_PAIR, REGISTER_DENIED, REGISTER_EXISTS
from app.app.services import login_codes
from app.app.tests.auth_helpers import GOOD_PASSWORD, login_via_code, register_via_code

GOOD = GOOD_PASSWORD


def _set_mode(monkeypatch, mode: str) -> None:
    from app.app.config import get_settings

    monkeypatch.setenv("PERSONAL_PASSWORD", mode)
    get_settings.cache_clear()


@pytest.fixture()
def required(client, monkeypatch):
    from app.app.services import user_profile

    _set_mode(monkeypatch, "required")
    monkeypatch.setattr(user_profile, "is_complete", lambda _profile: True)
    return client


def _register_to_code(client, name="Admin"):
    r = client.post("/auth/register", json={"name": name})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["step"] == "code"
    return data


def _login_to_code(client, name="Admin", tg="tg-admin", password=GOOD, role=None):
    body = {"name": name, "telegram_id": tg, "password": password}
    if role is not None:
        body["role"] = role
    r = client.post("/auth/login", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["step"] == "code"
    return data


def _send_and_verify(client, ticket, name, channel="b24", role=None):
    r = client.post("/auth/login/code/request", json={"ticket": ticket, "channel": channel})
    assert r.status_code == 200, r.text
    code = login_codes.peek_dev_code(name)
    assert code
    body = {"ticket": ticket, "code": code}
    if role is not None:
        body["role"] = role
    return client.post("/auth/login/code/verify", json=body)


def _setup(client, ticket, pwd=GOOD, confirm=None):
    return client.post(
        "/auth/login/setup",
        json={"ticket": ticket, "password": pwd, "password_confirm": pwd if confirm is None else confirm},
    )


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_off_mode_gives_session_after_code(client):
    data = login_via_code(client, name="Admin", telegram_id="tg-admin")
    assert "access_token" in data and "step" not in data


def test_register_then_login(required):
    client = required
    first = _register_to_code(client)
    verified = _send_and_verify(client, first["ticket"], "Admin")
    assert verified.status_code == 200, verified.text
    setup_step = verified.json()
    assert setup_step["step"] == "setup" and setup_step["min_length"] == 10

    r = _setup(client, setup_step["ticket"])
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"
    assert client.get("/projects", headers=_bearer(r.json()["access_token"])).status_code == 200

    again = _login_to_code(client, password=GOOD)
    verified2 = _send_and_verify(client, again["ticket"], "Admin")
    assert verified2.status_code == 200
    assert "access_token" in verified2.json()


def test_login_rejects_with_same_message(required):
    client = required
    register_via_code(client, name="Admin")
    cases = [
        {"name": "not-on-list-xyz", "password": GOOD},
        {"name": "Admin", "password": "nope"},
        {"name": "Manager", "password": GOOD},  # в списке, но без пароля
        {"name": "Admin", "password": ""},
    ]
    details = []
    for body in cases:
        r = client.post("/auth/login", json={**body, "telegram_id": "tg"})
        assert r.status_code == 401, body
        details.append(r.json()["detail"])
    assert details == [LOGIN_BAD_PAIR] * len(cases)


def test_register_allowlist_and_exists(required):
    client = required
    denied = client.post("/auth/register", json={"name": "stranger-xyz"})
    assert denied.status_code == 401
    assert denied.json()["detail"] == REGISTER_DENIED

    register_via_code(client, name="Admin")
    again = client.post("/auth/register", json={"name": "Admin"})
    assert again.status_code == 409
    assert again.json()["detail"] == REGISTER_EXISTS


def test_role_chosen_on_code_step(required, tmp_path, monkeypatch):
    from app.app.config import get_settings

    roles = tmp_path / "Login_roles_multi.txt"
    roles.write_text("Admin | admin,manager\n", encoding="utf-8")
    monkeypatch.setenv("LOGIN_ROLES_PATH", str(roles))
    get_settings.cache_clear()

    client = required
    first = _register_to_code(client)
    assert first.get("role") is None
    assert {r["role"] for r in first["roles"]} == {"admin", "manager"}

    r = client.post("/auth/login/code/request", json={"ticket": first["ticket"], "channel": "b24"})
    assert r.status_code == 200
    code = login_codes.peek_dev_code("Admin")
    denied = client.post(
        "/auth/login/code/verify",
        json={"ticket": first["ticket"], "code": code},
    )
    assert denied.status_code == 400
    assert "роль" in denied.json()["detail"].casefold()

    again = _register_to_code(client)
    setup = _send_and_verify(client, again["ticket"], "Admin", role="manager").json()
    assert setup["step"] == "setup"
    r = _setup(client, setup["ticket"])
    assert r.json()["role"] == "manager"


def test_wrong_personal_password_401(required):
    client = required
    register_via_code(client, name="Admin")
    r = client.post("/auth/login", json={"name": "Admin", "telegram_id": "tg-admin", "password": "nope"})
    assert r.status_code == 401
    assert r.json()["detail"] == LOGIN_BAD_PAIR
    assert "ticket" not in r.text


def test_setup_rules_and_mismatch(required):
    first = _register_to_code(required)
    ticket = _send_and_verify(required, first["ticket"], "Admin").json()["ticket"]
    r = _setup(required, ticket, GOOD, GOOD + "x")
    assert r.status_code == 400 and r.json()["detail"] == "Пароли не совпадают"
    r = _setup(required, ticket, "short")
    assert r.status_code == 400 and "короче" in r.json()["detail"]
    assert _setup(required, ticket).status_code == 200


def test_ticket_is_one_time(required):
    first = _register_to_code(required)
    ticket = _send_and_verify(required, first["ticket"], "Admin").json()["ticket"]
    assert _setup(required, ticket).status_code == 200
    again = _setup(required, ticket)
    assert again.status_code == 401


def test_ticket_expired_forged(required):
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    first = _register_to_code(required)
    ticket = _send_and_verify(required, first["ticket"], "Admin").json()["ticket"]
    claims = jwt.get_unverified_claims(ticket)
    old = dict(claims, exp=datetime.now(timezone.utc) - timedelta(seconds=1))
    expired = jwt.encode(old, "test-secret-key-32chars!!", algorithm="HS256")
    assert _setup(required, expired).status_code == 401
    forged = jwt.encode(claims, "other-secret-key-32chars!", algorithm="HS256")
    assert _setup(required, forged).status_code == 401


def test_ticket_is_not_a_session(required):
    first = _register_to_code(required)
    ticket = first["ticket"]
    r = required.get("/projects", headers=_bearer(ticket))
    assert r.status_code == 401


def test_customer_also_sets_personal_password(required):
    """Роль user тоже проходит регистрацию с личным паролем."""
    client = required
    data = register_via_code(client, name="User", channel="email")
    assert "access_token" in data
    assert data["role"] == "user"
    again = login_via_code(client, name="User", telegram_id="tg-user", password=GOOD, channel="email")
    assert "access_token" in again
