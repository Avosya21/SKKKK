"""Вход в два шага: пароль роли → личный пароль (PERSONAL_PASSWORD=required)."""

from __future__ import annotations

import pytest

from app.app.tests.conftest import ADMIN_PASS, MANAGER_PASS

GOOD = "correct-horse-battery"


def _set_mode(monkeypatch, mode: str) -> None:
    from app.app.config import get_settings

    monkeypatch.setenv("PERSONAL_PASSWORD", mode)
    get_settings.cache_clear()


@pytest.fixture()
def required(client, monkeypatch):
    from app.app.services import user_profile

    _set_mode(monkeypatch, "required")
    # Здесь проверяется только личный пароль; шаг «карточка» — в test_login_profile.py.
    monkeypatch.setattr(user_profile, "is_complete", lambda _profile: True)
    return client


def _step1(client, name="Admin", password=ADMIN_PASS, tg="tg-admin"):
    r = client.post("/auth/login", json={"name": name, "telegram_id": tg, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _setup(client, ticket, pwd=GOOD, confirm=None):
    return client.post(
        "/auth/login/setup",
        json={"ticket": ticket, "password": pwd, "password_confirm": pwd if confirm is None else confirm},
    )


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_off_mode_gives_session_right_away(client):
    data = _step1(client)
    assert "access_token" in data and "step" not in data


def test_first_login_setup_then_personal(required):
    client = required
    first = _step1(client)
    assert first["step"] == "setup" and first["min_length"] == 10
    assert "access_token" not in first
    assert "primary_analitics_session" not in client.cookies

    r = _setup(client, first["ticket"])
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"
    assert client.get("/projects", headers=_bearer(r.json()["access_token"])).status_code == 200

    second = _step1(client)
    assert second["step"] == "personal"
    bad = client.post("/auth/login/personal", json={"ticket": second["ticket"], "password": "wrong-one-here"})
    assert bad.status_code == 401 and bad.json()["detail"] == "Неверный личный пароль"
    ok = client.post("/auth/login/personal", json={"ticket": second["ticket"], "password": GOOD})
    assert ok.status_code == 200, ok.text


def test_role_still_comes_from_role_password(required):
    client = required
    _setup(client, _step1(client)["ticket"])
    step = _step1(client, password=MANAGER_PASS)
    r = client.post("/auth/login/personal", json={"ticket": step["ticket"], "password": GOOD})
    assert r.json()["role"] == "manager"


def test_wrong_role_password_reveals_nothing(required):
    r = required.post("/auth/login", json={"name": "Admin", "telegram_id": "tg", "password": "nope"})
    assert r.status_code == 401
    assert "step" not in r.text and "ticket" not in r.text


def test_setup_rules_and_mismatch(required):
    ticket = _step1(required)["ticket"]
    r = _setup(required, ticket, GOOD, GOOD + "x")
    assert r.status_code == 400 and r.json()["detail"] == "Пароли не совпадают"
    r = _setup(required, ticket, "short")
    assert r.status_code == 400 and "короче" in r.json()["detail"]
    # Пропуск не сгорел на ошибке правил — можно исправить и продолжить.
    assert _setup(required, ticket).status_code == 200


def test_ticket_is_one_time(required):
    ticket = _step1(required)["ticket"]
    assert _setup(required, ticket).status_code == 200
    again = required.post("/auth/login/personal", json={"ticket": ticket, "password": GOOD})
    assert again.status_code == 401


def test_ticket_expired_forged_or_session_token(required, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    ticket = _step1(required)["ticket"]
    claims = jwt.get_unverified_claims(ticket)
    old = dict(claims, exp=datetime.now(timezone.utc) - timedelta(seconds=1))
    expired = jwt.encode(old, "test-secret-key-32chars!!", algorithm="HS256")
    assert _setup(required, expired).status_code == 401
    forged = jwt.encode(claims, "other-secret-key-32chars!", algorithm="HS256")
    assert _setup(required, forged).status_code == 401
    wrong_purpose = jwt.encode(dict(claims, purpose="x"), "test-secret-key-32chars!!", algorithm="HS256")
    assert _setup(required, wrong_purpose).status_code == 401
    assert "Время на вход вышло" in _setup(required, "garbage").json()["detail"]


def test_ticket_is_not_a_session(required):
    ticket = _step1(required)["ticket"]
    assert required.get("/projects", headers=_bearer(ticket)).status_code == 401


def test_personal_step_is_rate_limited_and_step1_does_not_reset(required):
    client = required
    _setup(client, _step1(client)["ticket"])
    for _ in range(8):
        step = _step1(client)  # верный пароль роли не обнуляет счётчик имени
        r = client.post("/auth/login/personal", json={"ticket": step["ticket"], "password": "wrong-one-here"})
        assert r.status_code == 401
    r = client.post("/auth/login", json={"name": "Admin", "telegram_id": "tg-admin", "password": ADMIN_PASS})
    assert r.status_code == 429


def test_setup_when_password_exists_is_conflict(required):
    ticket_a = _step1(required)["ticket"]
    ticket_b = _step1(required)["ticket"]
    assert _setup(required, ticket_a).status_code == 200
    assert _setup(required, ticket_b).status_code == 409


def test_session_from_before_personal_passwords_dies_right_away(client, monkeypatch):
    old_token = _step1(client)["access_token"]  # выдан до включения: без pp и pf
    _set_mode(monkeypatch, "required")
    assert client.get("/projects", headers=_bearer(old_token)).status_code == 401


def test_change_password_logs_out_other_sessions(required):
    client = required
    first = _setup(client, _step1(client)["ticket"]).json()["access_token"]
    step = _step1(client)
    second = client.post("/auth/login/personal", json={"ticket": step["ticket"], "password": GOOD}).json()["access_token"]

    body = {"current_password": "wrong-current", "password": GOOD + "2", "password_confirm": GOOD + "2"}
    assert client.post("/auth/password/change", json=body, headers=_bearer(second)).status_code == 400
    body["current_password"] = GOOD
    r = client.post("/auth/password/change", json=body, headers=_bearer(second))
    assert r.status_code == 200, r.text
    fresh = r.json()["access_token"]
    assert client.get("/projects", headers=_bearer(first)).status_code == 401
    assert client.get("/projects", headers=_bearer(second)).status_code == 401
    assert client.get("/projects", headers=_bearer(fresh)).status_code == 200
    assert client.get("/auth/password/state", headers=_bearer(fresh)).json() == {
        "mode": "required",
        "has_password": True,
        "min_length": 10,
    }


def test_admin_reset_sends_user_to_setup(required):
    from app.app.services import personal_password

    token = _setup(required, _step1(required)["ticket"]).json()["access_token"]
    personal_password.reset("admin")
    assert required.get("/projects", headers=_bearer(token)).status_code == 401
    assert _step1(required)["step"] == "setup"


def test_change_is_refused_when_off(client):
    token = _step1(client)["access_token"]
    r = client.post(
        "/auth/password/change",
        json={"password": GOOD, "password_confirm": GOOD},
        headers=_bearer(token),
    )
    assert r.status_code == 409


def test_activity_log_has_no_passwords(required, db_engine):
    from sqlalchemy import text

    _setup(required, _step1(required)["ticket"])
    with db_engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM user_activity_log")).fetchall()
    assert rows
    assert GOOD not in repr(rows)


def test_required_is_default_and_optional_is_gone():
    from pydantic import ValidationError

    from app.app.config import Settings

    assert Settings.model_fields["personal_password"].default == "required"
    with pytest.raises(ValidationError, match="optional"):
        Settings(personal_password="optional")


def test_skip_endpoint_is_gone(required):
    ticket = _step1(required)["ticket"]
    assert required.post("/auth/login/skip", json={"ticket": ticket}).status_code in (404, 405)
