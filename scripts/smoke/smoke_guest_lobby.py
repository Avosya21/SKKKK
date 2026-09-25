"""Гостевой вход (пункты 3.3/3.12): кнопка «Войти во встречу» видна без прокрутки на типовых размерах."""
from playwright.sync_api import sync_playwright

from common import *

SIZES = [(1920, 1080), (1440, 900), (1366, 768), (1280, 720), (1024, 768), (768, 1024), (375, 812), (360, 640)]
c, _ = api_login("admin")
mid, tok = new_live_meeting(c)
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=CHROME_ARGS)
    bad = []
    for w, h in SIZES:
        ctx = b.new_context(ignore_https_errors=True, viewport={"width": w, "height": h})
        pg = ctx.new_page()
        pg.goto(f"{BASE}/m/{tok}")
        pg.wait_for_selector("#guest-join")
        box = pg.evaluate("(()=>{const r=document.getElementById('guest-join').getBoundingClientRect();return [Math.round(r.top),Math.round(r.bottom),innerHeight]})()")
        visible = box[1] <= box[2]
        print(("PASS " if visible else "FAIL ") + f"{w}x{h}: кнопка bottom={box[1]} из {box[2]}")
        if not visible:
            bad.append(f"{w}x{h}")
        ctx.close()
    b.close()
c.post(f"/projects/{PID}/meetings/{mid}/end")
check(not bad, f"кнопка видна без прокрутки на всех размерах {bad}")
