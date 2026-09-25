"""Общие настройки смоук-набора. Всё берётся из переменных окружения — секретов в коде нет.

SMOKE_BASE         адрес API (по умолчанию https://127.0.0.1:777)
SMOKE_PROJECT_ID   id ПЕСОЧНИЦЫ (по умолчанию 29). Клиентские проекты трогать нельзя.
SMOKE_LOGIN        логин из allowlist (по умолчанию user2@example.com)
SMOKE_ADMIN_PW / SMOKE_MANAGER_PW / SMOKE_USER_PW   пароли ролей (тестовых учёток)
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("SMOKE_BASE", "https://127.0.0.1:777").rstrip("/")
PID = int(os.environ.get("SMOKE_PROJECT_ID", "29"))
LOGIN = os.environ.get("SMOKE_LOGIN", "user2@example.com")
TG = os.environ.get("SMOKE_TG", "smoke")
CHROME_ARGS = [
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
]


def password(role: str) -> str:
    value = os.environ.get(f"SMOKE_{role.upper()}_PW", "")
    if not value:
        print(f"SKIP: не задана переменная SMOKE_{role.upper()}_PW")
        sys.exit(77)
    return value


def api_login(role: str = "admin") -> tuple[httpx.Client, dict]:
    client = httpx.Client(base_url=BASE, verify=False, timeout=60)
    r = client.post("/auth/login", json={"name": LOGIN, "telegram_id": TG, "password": password(role)})
    r.raise_for_status()
    data = r.json()
    client.headers["Authorization"] = "Bearer " + data["access_token"]
    return client, data


def new_live_meeting(client: httpx.Client) -> tuple[int, str]:
    m = client.post(f"/projects/{PID}/meetings").json()
    client.post(f"/projects/{PID}/meetings/{m['id']}/start").raise_for_status()
    return m["id"], m["public_token"]


def browser_login(page, role: str = "admin") -> None:
    page.goto(BASE + "/login")
    ins = page.locator("form input")
    ins.nth(0).fill(LOGIN)
    ins.nth(1).fill(TG)
    ins.nth(2).fill(password(role))
    page.locator("form button[type=submit]").click()
    page.wait_for_url(lambda u: "/login" not in u, timeout=15000)


def check(cond: bool, message: str) -> None:
    print(("PASS " if cond else "FAIL ") + message)
    if not cond:
        raise AssertionError(message)


def wait_until(fn, timeout: float = 15.0, step: float = 0.5):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(step)
    return fn()
