"""Нет ошибок консоли и упавших запросов на основных страницах (пункт 3.6)."""
import time

from playwright.sync_api import sync_playwright

from common import *

# Известный шум LiveKit/браузера, не относящийся к нашему коду (расширять осознанно):
WHITELIST = ("favicon", "Download the React DevTools")
c, _ = api_login("admin")
mid, tok = new_live_meeting(c)
found: dict[str, list[str]] = {}


current = {}


def watch(page, tag):
    """Один набор обработчиков на страницу; вкладка (tag) меняется через current[page]."""
    found[tag] = []
    fresh = id(page) not in current
    current[id(page)] = tag
    if not fresh:
        return
    add = lambda text: found[current[id(page)]].append(text)  # noqa: E731
    page.on("console", lambda m: add(f"console.{m.type}: {m.text[:140]}") if m.type == "error" else None)
    page.on("pageerror", lambda e: add(f"pageerror: {str(e)[:140]}"))
    page.on("response", lambda r: add(f"HTTP {r.status} {r.url.replace(BASE, '')[:90]}") if r.status >= 400 else None)


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=CHROME_ARGS)
    anon = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"]).new_page()
    watch(anon, "login")
    anon.goto(BASE + "/login"); time.sleep(2)
    watch_g = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"]).new_page()
    watch(watch_g, "guest")
    watch_g.goto(f"{BASE}/m/{tok}"); time.sleep(2)
    staff = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"]).new_page()
    browser_login(staff, "admin")
    for tag, url, wait in (("index", "/", 2), ("project", f"/projects/{PID}", 4), ("room", f"/projects/{PID}/meetings/{mid}/room", 6)):
        watch(staff, tag)
        staff.goto(BASE + url); time.sleep(wait)
    b.close()
c.post(f"/projects/{PID}/meetings/{mid}/end")
bad = {}
for tag, items in found.items():
    real = [i for i in items if not any(w in i for w in WHITELIST)]
    if real:
        bad[tag] = real[:4]
    print(("PASS " if not real else "FAIL ") + f"{tag}: {len(real)} проблем {real[:2]}")
check(not bad, f"консоль и запросы чистые {bad}")
