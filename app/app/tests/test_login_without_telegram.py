"""Вход без Telegram ID (решение владельца 2026-09-28): поле на форме скрыто — уведомлений в Telegram нет (бот не настроен),
учётку определяет логин. Указанный Telegram ID по-прежнему пишется на учётку; без него остаётся прежний, у новой учётки —
заглушка по логину (столбец обязательный и уникальный)."""
from __future__ import annotations

from app.app.tests.conftest import ADMIN_PASS, USER_PASS


def _telegram_id(name: str) -> str:
    from app.app.db import SessionLocal
    from app.app.models.user import User
    from app.app.services.user_identity import find_users_by_login_name

    db = SessionLocal()
    try:
        return find_users_by_login_name(db, name)[0].telegram_id
    finally:
        db.close()


def test_existing_user_logs_in_without_telegram_and_keeps_it(client, admin_token):
    r = client.post("/auth/login", json={"name": "Admin", "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    assert _telegram_id("Admin") == "tg-admin"  # прежний не затёрт
    r = client.post("/auth/login", json={"name": "Admin", "telegram_id": "  ", "password": ADMIN_PASS})
    assert r.status_code == 200 and _telegram_id("Admin") == "tg-admin"  # пустое — как не указано


def test_new_user_without_telegram_gets_placeholder(client):
    from app.app.services.user_identity import placeholder_telegram_id

    r = client.post("/auth/login", json={"name": "Bootstrap user", "password": USER_PASS})
    assert r.status_code == 200, r.text
    assert _telegram_id("Bootstrap user") == placeholder_telegram_id("Bootstrap user") == "pending-bootstrap user"
    r = client.post("/auth/login", json={"name": "Bootstrap user", "telegram_id": "123456", "password": USER_PASS})
    assert r.status_code == 200 and _telegram_id("Bootstrap user") == "123456"  # указанный — пишется, как раньше


def test_login_still_needs_name(client):
    assert client.post("/auth/login", json={"name": " ", "password": ADMIN_PASS}).json()["detail"] == "Name required"


def test_login_page_hides_telegram_field(client):
    html = client.get("/login").text
    field = html[html.index('<div class="mb-3" hidden>'):]
    assert 'id="login-telegram"' in field[:400] and "disabled" in field[:400] and "required" not in field[:400]
