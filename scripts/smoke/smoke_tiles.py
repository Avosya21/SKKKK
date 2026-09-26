"""Плитки комнаты (пункт 3.11): N участников × размеры окна — нет пересечений, выходов за экран и перекрытия панелью.

SMOKE_TILES — число участников (по умолчанию 6, включая сотрудника).
"""
import os
import time

from playwright.sync_api import sync_playwright

from common import *

N = int(os.environ.get("SMOKE_TILES", "6"))
SIZES = [(1920, 1080), (1440, 900), (1366, 768), (1280, 720), (1280, 600), (1024, 768), (900, 700), (800, 600)]
MEASURE = """
() => {
  const vw = innerWidth, vh = innerHeight;
  const tiles = Array.from(document.querySelectorAll('#meeting-tiles .meeting-tile')).filter(t => t.getBoundingClientRect().width > 0);
  const R = tiles.map(t => t.getBoundingClientRect());
  let overlaps = 0;
  for (let i = 0; i < R.length; i++) for (let j = i + 1; j < R.length; j++) {
    const a = R[i], b = R[j];
    if (a.left < b.right - 3 && b.left < a.right - 3 && a.top < b.bottom - 3 && b.top < a.bottom - 3) overlaps++; }
  const dock = document.getElementById('meeting-dock'); const dr = dock ? dock.getBoundingClientRect() : null;
  const outside = R.filter(r => r.right > vw + 1 || r.left < -1 || r.bottom > vh + 1 || r.top < -1).length;
  const behindDock = dr ? R.filter(r => r.bottom > dr.top + 2 && r.top < dr.bottom).length : 0;
  const minW = R.length ? Math.round(Math.min(...R.map(r => r.width))) : 0;
  return { tiles: R.length, overlaps, outside, behindDock, minTileW: minW,
           pageOverflowY: document.documentElement.scrollHeight > vh + 1 };
}
"""

c, _ = api_login("admin")
mid, tok = new_live_meeting(c)
problems = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=CHROME_ARGS)
    sctx = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"], viewport={"width": 1440, "height": 900})
    sp = sctx.new_page()
    browser_login(sp, "admin")
    sp.goto(f"{BASE}/projects/{PID}/meetings/{mid}/room")
    sp.wait_for_selector("#meeting-stage", timeout=20000)
    for i in range(N - 1):
        gctx = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"], viewport={"width": 800, "height": 600})
        gp = gctx.new_page()
        gp.goto(f"{BASE}/m/{tok}")
        gp.fill("#guest-name", f"Гость {i + 1} с длинным именем")
        gp.click("#guest-join")
        gp.wait_for_selector("#meeting-stage", state="visible", timeout=25000)
        time.sleep(1)
    wait_until(lambda: sp.evaluate("document.querySelectorAll('#meeting-tiles .meeting-tile').length") >= N, 25)
    for w, h in SIZES:
        sp.set_viewport_size({"width": w, "height": h})
        time.sleep(1.5)
        m = sp.evaluate(MEASURE)
        ok = m["tiles"] >= N and m["overlaps"] == 0 and m["outside"] == 0 and m["behindDock"] == 0
        print(("PASS " if ok else "FAIL ") + f"{N} участников, {w}x{h}: {m}")
        if not ok:
            problems.append(f"{w}x{h}")
    b.close()
c.post(f"/projects/{PID}/meetings/{mid}/end")
check(not problems, f"плитки {N} участников корректны на всех размерах {problems}")
