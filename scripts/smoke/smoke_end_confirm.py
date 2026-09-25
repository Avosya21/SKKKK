"""«Завершить для всех» (пункт 3.13): своё окно подтверждения; «Отмена» встречу не завершает, «Завершить» — завершает."""
import time

from playwright.sync_api import sync_playwright

from common import *

c, _ = api_login("admin")
mid, tok = new_live_meeting(c)
status = lambda: c.get(f"/projects/{PID}/meetings/{mid}").json()["status"]  # noqa: E731
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=CHROME_ARGS)
    ctx = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"], viewport={"width": 1280, "height": 720})
    pg = ctx.new_page()
    native = []
    pg.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
    browser_login(pg, "admin")
    pg.goto(f"{BASE}/projects/{PID}/meetings/{mid}/room")
    pg.wait_for_selector("#meeting-stage", timeout=20000)
    wait_until(lambda: (pg.evaluate("(document.getElementById('meeting-people')||{}).textContent") or "").startswith("В комнате"), 15)
    people = pg.evaluate("(document.getElementById('meeting-people')||{}).textContent") or ""
    check(people.startswith("В комнате ("), f"строка «В комнате» со счётчиком: {people!r}")
    pg.click("#meeting-end")
    pg.wait_for_selector(".meeting-confirm", timeout=3000)
    check(not native, "системный confirm не используется")
    pg.click(".meeting-confirm [data-act=no]")
    time.sleep(1)
    check(status() == "live" and pg.locator(".meeting-confirm").count() == 0, "«Отмена»: встреча идёт, окно закрыто")
    pg.click("#meeting-end")
    pg.wait_for_selector(".meeting-confirm")
    pg.click(".meeting-confirm [data-act=yes]")
    check(bool(wait_until(lambda: status() == "ended", 10)), "«Завершить для всех»: встреча завершена")
    b.close()
