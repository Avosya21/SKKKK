"""Двойной клик = один запрос (пункт 3.18): «Создать встречу» и «Пересобрать» ТЗ. Только песочница."""
import time

from playwright.sync_api import sync_playwright

from common import *

c, _ = api_login("admin")
before = {m["id"] for m in c.get(f"/projects/{PID}/meetings").json()}
posts: list[str] = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=CHROME_ARGS)
    ctx = b.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    pg.on("request", lambda r: posts.append(r.url.replace(BASE, "")) if r.method == "POST" else None)
    browser_login(pg, "admin")
    pg.goto(f"{BASE}/projects/{PID}")
    pg.wait_for_selector("#meetings-create", state="attached")
    pg.evaluate("document.getElementById('meetings-panel').classList.add('show')")
    time.sleep(0.8)
    posts.clear()
    pg.dblclick("#meetings-create")
    time.sleep(2)
    creates = [u for u in posts if u.endswith(f"/projects/{PID}/meetings")]
    check(len(creates) == 1, f"«Создать встречу»: двойной клик = {len(creates)} запрос(а)")
    posts.clear()
    btn = pg.locator("#tz-refresh")
    if btn.count():
        btn.dblclick()
        time.sleep(2)
        rebuilds = [u for u in posts if u.endswith("/rebuild-tz")]
        check(len(rebuilds) <= 1, f"«Пересобрать»: двойной клик = {len(rebuilds)} запрос(а)")
    b.close()
after = c.get(f"/projects/{PID}/meetings").json()
for m in after:
    if m["id"] not in before:
        c.delete(f"/projects/{PID}/meetings/{m['id']}")
