"""Общий вход для тестов: логин/регистрация → код (dev) → сессия / следующий шаг."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.app.services import login_codes, personal_password

GOOD_PASSWORD = "correct-horse-battery"


def _complete_code(
    client: TestClient,
    data: dict,
    *,
    name: str,
    role: str | None = None,
    channel: str | None = None,
    setup_password: str | None = GOOD_PASSWORD,
) -> dict:
    """От code-step до access_token / profile / ответа setup (если пароль не задаём)."""
    assert data.get("step") == "code", data
    ticket = data["ticket"]
    ch = channel or data.get("channel") or "b24"
    r2 = client.post("/auth/login/code/request", json={"ticket": ticket, "channel": ch})
    assert r2.status_code == 200, r2.text
    code = login_codes.peek_dev_code(name)
    assert code, "dev-код не выдан"
    verify: dict = {"ticket": ticket, "code": code}
    if role is not None:
        verify["role"] = role
    elif len(data.get("roles") or []) > 1:
        verify["role"] = data["roles"][0]["role"]
    r3 = client.post("/auth/login/code/verify", json=verify)
    assert r3.status_code == 200, r3.text
    out = r3.json()
    if out.get("step") == "setup" and setup_password is not None:
        r4 = client.post(
            "/auth/login/setup",
            json={
                "ticket": out["ticket"],
                "password": setup_password,
                "password_confirm": setup_password,
            },
        )
        assert r4.status_code == 200, r4.text
        return r4.json()
    return out


def register_via_code(
    client: TestClient,
    *,
    name: str,
    password: str = GOOD_PASSWORD,
    role: str | None = None,
    channel: str | None = None,
) -> dict:
    """Первый вход: /auth/register → код → setup пароля → дальше."""
    r = client.post("/auth/register", json={"name": name})
    assert r.status_code == 200, r.text
    return _complete_code(
        client, r.json(), name=name, role=role, channel=channel, setup_password=password
    )


def login_via_code(
    client: TestClient,
    *,
    name: str,
    telegram_id: str = "tg",
    password: str | None = None,
    role: str | None = None,
    channel: str | None = None,
) -> dict:
    """Полный вход до ответа с access_token или step setup/profile.

    При ``PERSONAL_PASSWORD=required`` без пароля сначала регистрирует (если ещё нет),
    иначе логинится с паролем. В режиме ``off`` пароль не нужен.
    """
    if personal_password.enabled():
        if password is None:
            if not personal_password.has_password(name):
                return register_via_code(client, name=name, role=role, channel=channel)
            password = GOOD_PASSWORD
        body: dict = {"name": name, "telegram_id": telegram_id, "password": password}
        r = client.post("/auth/login", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        if "access_token" in data:
            return data
        return _complete_code(
            client, data, name=name, role=role, channel=channel, setup_password=None
        )

    body = {"name": name, "telegram_id": telegram_id}
    if password is not None:
        body["password"] = password
    r = client.post("/auth/login", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    if "access_token" in data:
        return data
    return _complete_code(
        client, data, name=name, role=role, channel=channel, setup_password=None
    )


def access_token(client: TestClient, **kwargs) -> str:
    data = login_via_code(client, **kwargs)
    assert "access_token" in data, data
    return data["access_token"]
