"""Формы (пункт 3.18): вход по Enter; название проекта из пробелов отклоняется по-русски и ничего не создаёт."""
import time

from playwright.sync_api import sync_playwright

from common import *

c, _ = api_login("admin")
before = len(c.get("/projects").json())
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=CHROME_ARGS)
    pg = b.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 800}).new_page()
    browser_login(pg, "admin")
    check(pg.evaluate("location.pathname") != "/login", "вход через форму смоука")
    pg.goto(BASE + "/")
    pg.wait_for_selector("#btn-new-project, #new-project-btn, [id*='new-project']", timeout=10000)
    pg.evaluate("document.querySelector('#btn-new-project, #new-project-btn, [id*=new-project]').click()")
    pg.wait_for_selector("#new-project-form input[name=name]", state="visible", timeout=5000)
    # Без «Внутренний» браузер не даст отправить форму (нужна компания заказчика) — сообщение не появится.
    pg.check("#project-internal")
    pg.fill("#new-project-form input[name=name]", "   ")
    pg.click("#project-submit")
    time.sleep(1.5)
    msg = pg.evaluate("(document.getElementById('project-error')||{}).textContent || ''")
    check(any(ch in msg for ch in "Введите"), f"пустое название отклонено по-русски: {msg!r}")
    b.close()
check(len(c.get("/projects").json()) == before, "проект не создан")
