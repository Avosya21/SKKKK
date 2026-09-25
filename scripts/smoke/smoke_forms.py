"""Формы (пункт 3.18): вход по Enter; название проекта из пробелов отклоняется по-русски и ничего не создаёт."""
import time

from playwright.sync_api import sync_playwright

from common import *

c, _ = api_login("admin")
before = len(c.get("/projects").json())
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=CHROME_ARGS)
    pg = b.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 800}).new_page()
    pg.goto(BASE + "/login")
    ins = pg.locator("form input")
    ins.nth(0).fill(LOGIN); ins.nth(1).fill(TG); ins.nth(2).fill(password("admin"))
    ins.nth(2).press("Enter")
    check(bool(wait_until(lambda: pg.evaluate("location.pathname") != "/login", 10)), "вход отправляется по Enter")
    pg.goto(BASE + "/")
    pg.wait_for_selector("#new-project-btn, [id*='new-project']", timeout=10000)
    pg.evaluate("document.querySelector('#new-project-btn, [id*=new-project]').click()")
    pg.wait_for_selector("#new-project-form input[name=name]", state="visible", timeout=5000)
    pg.fill("#new-project-form input[name=name]", "   ")
    pg.press("#new-project-form input[name=name]", "Enter")
    time.sleep(1.5)
    msg = pg.evaluate("(document.getElementById('project-error')||{}).textContent || ''")
    check(any(ch in msg for ch in "Введите"), f"пустое название отклонено по-русски: {msg!r}")
    b.close()
check(len(c.get("/projects").json()) == before, "проект не создан")
