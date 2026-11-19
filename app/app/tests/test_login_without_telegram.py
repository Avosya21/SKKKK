"""Вход без Telegram ID (решение владельца 2026-09-28)."""
from __future__ import annotations

from app.app.tests.auth_helpers import login_via_code


def _telegram_id(name: str) -> str:
    from app.app.db import SessionLocal
    from app.app.services.user_identity import find_users_by_login_name

    db = SessionLocal()
    try:
        return find_users_by_login_name(db, name)[0].telegram_id
    finally:
        db.close()


def test_existing_user_logs_in_without_telegram_and_keeps_it(client, admin_token):
    # Не передаём telegram_id — прежний tg-admin не должен затираться.
    body = {"name": "Admin"}
    r = client.post("/auth/login", json=body)
    assert r.status_code == 200 and r.json()["step"] == "code"
    from app.app.services import login_codes

    ticket = r.json()["ticket"]
    assert client.post("/auth/login/code/request", json={"ticket": ticket, "channel": "b24"}).status_code == 200
    code = login_codes.peek_dev_code("Admin")
    assert client.post("/auth/login/code/verify", json={"ticket": ticket, "code": code}).status_code == 200
    assert _telegram_id("Admin") == "tg-admin"
    r2 = client.post("/auth/login", json={"name": "Admin", "telegram_id": "  "})
    assert r2.status_code == 200
    ticket2 = r2.json()["ticket"]
    assert client.post("/auth/login/code/request", json={"ticket": ticket2, "channel": "b24"}).status_code == 200
    code2 = login_codes.peek_dev_code("Admin")
    assert client.post("/auth/login/code/verify", json={"ticket": ticket2, "code": code2}).status_code == 200
    assert _telegram_id("Admin") == "tg-admin"


def test_new_user_without_telegram_gets_placeholder(client):
    from app.app.services.user_identity import placeholder_telegram_id

    login_via_code(client, name="Bootstrap user", telegram_id="")
    assert _telegram_id("Bootstrap user") == placeholder_telegram_id("Bootstrap user") == "pending-bootstrap user"
    login_via_code(client, name="Bootstrap user", telegram_id="123456")
    assert _telegram_id("Bootstrap user") == "123456"


def test_login_still_needs_name(client):
    assert client.post("/auth/login", json={"name": " "}).json()["detail"] == "Name required"


def test_login_page_hides_telegram_field(client):
    html = client.get("/login").text
    field = html[html.index('<div class="mb-3" hidden>'):]
    assert 'id="login-telegram"' in field[:400] and "disabled" in field[:400] and "required" not in field[:400]


def test_login_page_locks_send_until_resend(client):
    """После «Отправить код» кнопка остаётся заблокированной; «ещё раз» — ошибка / 1 мин."""
    html = client.get("/login").text
    assert 'id="code-send"' in html
    assert 'id="code-resend"' in html
    assert "Отправить код ещё раз" in html
    assert "RESEND_AFTER_MS = 60000" in html
    assert "function lockSendButton()" in html
    assert "function scheduleResendButton()" in html
    assert "fromResend" in html
    # Регрессия: раньше finally снова включал #code-send сразу после успеха.
    assert "sendBtn.disabled = false;\n      sendBtn.textContent = idle;" not in html
    assert "finally {\n      sendBtn.disabled = false;" not in html
