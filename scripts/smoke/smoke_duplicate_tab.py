"""Дубль-вкладка сотрудника (пункт 2.6): первая вкладка получает понятную причину и заблокированные кнопки."""
from playwright.sync_api import sync_playwright

from common import *

c, _ = api_login("admin")
mid, tok = new_live_meeting(c)
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=CHROME_ARGS)
    ctx = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"])
    first = ctx.new_page()
    browser_login(first, "admin")
    room = f"{BASE}/projects/{PID}/meetings/{mid}/room"
    first.goto(room)
    first.wait_for_selector("#meeting-stage", timeout=20000)
    wait_until(lambda: first.evaluate("!!(PrimaryAnaliticsMeeting.room && PrimaryAnaliticsMeeting._joined)"), 15)
    second = ctx.new_page()
    second.goto(room)
    second.wait_for_selector("#meeting-stage", timeout=20000)

    def status():
        return first.evaluate("(document.getElementById('meeting-room-status')||{}).textContent") or ""

    check(bool(wait_until(lambda: "другой вкладки" in status(), 20)), f"первая вкладка объясняет причину: {status()[:70]!r}")
    check(first.evaluate("document.getElementById('meeting-mic').disabled"), "кнопки первой вкладки заблокированы")
    check(first.locator("#meeting-duplicate-return").count() == 1, "есть кнопка «Вернуться сюда»")
    b.close()
c.post(f"/projects/{PID}/meetings/{mid}/end")
