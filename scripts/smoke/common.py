"""Общие настройки смоук-набора. Всё берётся из переменных окружения — секретов в коде нет.

SMOKE_BASE         адрес API (по умолчанию https://127.0.0.1:777)
SMOKE_PROJECT_ID   id ПЕСОЧНИЦЫ (по умолчанию 29). Клиентские проекты трогать нельзя.
SMOKE_LOGIN        логин из allowlist (по умолчанию — первый логин из Login_up.txt)
SMOKE_ROLE         роль сессии (admin|manager|user), по умолчанию admin
SMOKE_PERSONAL_PW  личный пароль смоук-логина (сотрудник). Для роли user не нужен.
SMOKE_OTP_CODE     код из B24/email после запроса.
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


def personal_password() -> str:
    value = os.environ.get("SMOKE_PERSONAL_PW", "")
    if not value:
        print("SKIP: вход требует личный пароль — задайте SMOKE_PERSONAL_PW для " + LOGIN)
        sys.exit(77)
    return value


def _skip_setup(step: str) -> None:
    what = "личный пароль" if step == "setup" else "карточку"
    print(
        f"SKIP: у смоук-логина {LOGIN} нет {'личного пароля' if step == 'setup' else 'карточки'} — "
        f"войдите им один раз в браузере и заполните {what}"
    )
    sys.exit(77)


def _mint_bearer(role: str) -> str:
    """JWT нужной роли без OTP (только при E2E_ALLOW_MINT=1)."""
    if os.environ.get("E2E_ALLOW_MINT", "").strip() not in {"1", "true", "yes"}:
        raise RuntimeError("нужен токен роли %s — задайте E2E_ALLOW_MINT=1 или SMOKE_OTP_CODE" % role)
    env = {**os.environ, "SMOKE_ROLE": role, "E2E_ALLOW_MINT": "1"}
    if LOGIN:
        env["SMOKE_LOGIN"] = LOGIN
    from pathlib import Path
    import subprocess

    mint = Path(__file__).resolve().parents[1] / "e2e" / "mint_smoke_token.py"
    r = subprocess.run(
        [sys.executable, str(mint)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "mint failed").strip())
    token = (r.stdout or "").strip().splitlines()[-1].strip()
    if not token:
        raise RuntimeError("mint_smoke_token вернул пустой токен")
    return token


def _bearer_role(token: str) -> str | None:
    """Роль из JWT без проверки подписи (только для сверки смоука)."""
    try:
        import base64
        import json

        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
        role = payload.get("role")
        return role if isinstance(role, str) else None
    except Exception:  # noqa: BLE001
        return None


def api_login(role: str = "admin") -> tuple[httpx.Client, dict]:
    """Вход: логин (+ роль, + личный пароль) → запрос кода → verify по SMOKE_OTP_CODE.

    Если задан ``SMOKE_BEARER_TOKEN`` (например из ``scripts/e2e/mint_smoke_token.py``),
    OTP не нужен. Токен должен быть той же роли, что просят; иначе при
    ``E2E_ALLOW_MINT=1`` выпускается отдельный JWT на запрошенную роль
    (smoke_perm: admin и manager на одной учётке).
    """
    client = httpx.Client(base_url=BASE, verify=False, timeout=60, headers={"Origin": BASE, "Referer": BASE + "/"})
    # Аргумент role важнее SMOKE_ROLE: smoke_perm зовёт admin и manager подряд на одной учётке.
    want_role = role or os.environ.get("SMOKE_ROLE") or "admin"
    bearer = os.environ.get("SMOKE_BEARER_TOKEN", "").strip()
    if bearer:
        have = _bearer_role(bearer)
        if have != want_role:
            bearer = _mint_bearer(want_role)
        client.headers["Authorization"] = "Bearer " + bearer
        return client, {"access_token": bearer, "role": want_role, "name": LOGIN}
    body: dict = {"name": LOGIN, "telegram_id": TG, "role": want_role}
    if want_role != "user":
        body["password"] = personal_password()
    r = client.post("/auth/login", json=body)
    r.raise_for_status()
    data = r.json()
    if data.get("step") == "code":
        channel = data.get("channel") or ("email" if want_role == "user" else "b24")
        r = client.post("/auth/login/code/request", json={"ticket": data["ticket"], "channel": channel})
        r.raise_for_status()
        code = os.environ.get("SMOKE_OTP_CODE", "").strip()
        if not code:
            print("SKIP: задайте SMOKE_OTP_CODE (код из Битрикс24 или письма)")
            sys.exit(77)
        r = client.post("/auth/login/code/verify", json={"ticket": data["ticket"], "code": code})
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
    want_role = os.environ.get("SMOKE_ROLE") or role
    bearer = os.environ.get("SMOKE_BEARER_TOKEN", "").strip()
    if bearer:
        # JWT без OTP (mint_smoke_token / E2E_ALLOW_MINT): cookie для HTML-роутов + localStorage для API.
        from urllib.parse import urlparse

        host = urlparse(BASE).hostname or "127.0.0.1"
        page.context.add_cookies(
            [
                {
                    "name": "primary_analitics_session",
                    "value": bearer,
                    "domain": host,
                    "path": "/",
                    "httpOnly": True,
                    "secure": BASE.startswith("https"),
                    "sameSite": "Lax",
                }
            ]
        )
        page.goto(BASE + "/login")
        page.evaluate(
            """([token, role, name]) => {
              localStorage.setItem('primary_analitics_token', token);
              localStorage.setItem('primary_analitics_role', role || 'admin');
              if (name) localStorage.setItem('primary_analitics_name', name);
            }""",
            [bearer, want_role, LOGIN or ""],
        )
        page.goto(BASE + "/projects")
        page.wait_for_function("location.pathname !== '/login'", timeout=15000)
        return
    page.goto(BASE + "/login")
    page.fill("#login-name", LOGIN)
    page.wait_for_timeout(400)
    tg = page.locator("#login-telegram")
    if tg.count() and tg.is_visible() and tg.is_enabled():
        tg.fill(TG)
    role_radio = page.locator(f'input[name="role"][value="{want_role}"]')
    if role_radio.count() and role_radio.is_visible():
        role_radio.check()
    pwd = page.locator("#login-password-group")
    if pwd.count() and pwd.is_visible():
        page.fill("#login-password", personal_password())
    page.locator("#login-form button[type=submit]").click()
    page.wait_for_selector("#code-form:not(.d-none)", timeout=15000)
    code = os.environ.get("SMOKE_OTP_CODE", "").strip()
    if not code:
        print("SKIP: задайте SMOKE_OTP_CODE (код из Битрикс24 или письма)")
        sys.exit(77)
    page.fill("#code-input", code)
    page.locator("#code-submit").click()
    page.wait_for_function(
        "location.pathname !== '/login' || !document.getElementById('step-form').classList.contains('d-none')"
        " || !document.getElementById('profile-form').classList.contains('d-none')",
        timeout=15000,
    )
    if page.evaluate("location.pathname") == "/login":
        if not page.locator("#profile-form").is_hidden() or not page.locator("#step-form").is_hidden():
            _skip_setup("profile" if not page.locator("#profile-form").is_hidden() else "setup")
    page.wait_for_function("location.pathname !== '/login'", timeout=15000)


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
