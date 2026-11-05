"""Общие настройки смоук-набора. Всё берётся из переменных окружения — секретов в коде нет.

SMOKE_BASE         адрес API (по умолчанию https://127.0.0.1:777)
SMOKE_PROJECT_ID   id ПЕСОЧНИЦЫ (по умолчанию 29). Клиентские проекты трогать нельзя.
SMOKE_LOGIN        логин из allowlist (по умолчанию — первый логин из Login_up.txt)
SMOKE_ADMIN_PW / SMOKE_MANAGER_PW / SMOKE_USER_PW   пароли ролей (тестовых учёток)
SMOKE_PERSONAL_PW  личный пароль смоук-логина (второй шаг входа). Личный пароль и карточку смоук-логина
                   один раз заводят руками в браузере; без них смоук пропускается (код 77) с подсказкой.
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("SMOKE_BASE", "https://127.0.0.1:777").rstrip("/")
PID = int(os.environ.get("SMOKE_PROJECT_ID", "29"))
def _first_allowed_login() -> str:
    """Первый логин из Login_up.txt (файл вне git): логины сотрудников в код не пишем."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "Login_up.txt"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return ""


LOGIN = os.environ.get("SMOKE_LOGIN") or _first_allowed_login()
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


def personal_password() -> str:
    value = os.environ.get("SMOKE_PERSONAL_PW", "")
    if not value:
        print("SKIP: вход требует личный пароль — задайте SMOKE_PERSONAL_PW для " + LOGIN)
        sys.exit(77)
    return value


def _skip_setup(step: str) -> None:
    what = "личный пароль" if step == "setup" else "карточку"
    print(f"SKIP: у смоук-логина {LOGIN} нет {'личного пароля' if step == 'setup' else 'карточки'} — "
          f"войдите им один раз в браузере и заполните {what}")
    sys.exit(77)


def api_login(role: str = "admin") -> tuple[httpx.Client, dict]:
    # Origin нужен CSRF-middleware: после login в клиенте есть cookie сессии.
    client = httpx.Client(base_url=BASE, verify=False, timeout=60, headers={"Origin": BASE, "Referer": BASE + "/"})
    r = client.post("/auth/login", json={"name": LOGIN, "telegram_id": TG, "password": password(role)})
    r.raise_for_status()
    data = r.json()
    if data.get("step") == "personal":
        r = client.post("/auth/login/personal", json={"ticket": data["ticket"], "password": personal_password()})
        r.raise_for_status()
        data = r.json()
    if data.get("step"):
        _skip_setup(data["step"])
    client.headers["Authorization"] = "Bearer " + data["access_token"]
    return client, data


def new_live_meeting(client: httpx.Client) -> tuple[int, str]:
    m = client.post(f"/projects/{PID}/meetings").json()
    client.post(f"/projects/{PID}/meetings/{m['id']}/start").raise_for_status()
    return m["id"], m["public_token"]


def browser_login(page, role: str = "admin") -> None:
    page.goto(BASE + "/login")
    page.fill("#login-name", LOGIN)
    # Telegram ID на форме сейчас скрыт (hide_telegram) — поле disabled, FormData его не шлёт.
    tg = page.locator("#login-telegram")
    if tg.count() and tg.is_visible() and tg.is_enabled():
        tg.fill(TG)
    page.fill("#login-password", password(role))
    page.locator("#login-form button[type=submit]").click()
    # Второй шаг (личный пароль) — если сервер его требует.
    page.wait_for_function(
        "location.pathname !== '/login' || !document.getElementById('step-form').classList.contains('d-none')"
        " || !document.getElementById('profile-form').classList.contains('d-none')",
        timeout=15000,
    )
    if page.evaluate("location.pathname") == "/login":
        if not page.locator("#profile-form").is_hidden() or not page.locator("#step-setup-group").is_hidden():
            _skip_setup("profile" if not page.locator("#profile-form").is_hidden() else "setup")
        page.fill("#step-personal", personal_password())
        page.locator("#step-submit").click()
    page.wait_for_function(
        "location.pathname !== '/login' || !document.getElementById('profile-form').classList.contains('d-none')",
        timeout=15000,
    )
    if page.evaluate("location.pathname") == "/login":
        _skip_setup("profile")


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
