"""Комната: два участника, завершение встречи замечается, кнопка смены камеры (браузер)."""
from playwright.sync_api import sync_playwright

from common import *

STUB = """(() => { const real = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
 navigator.mediaDevices.enumerateDevices = async () => { const l = await real();
   if (l.filter(d => d.kind === 'videoinput').length >= 2) return l;
   return l.concat([{kind:'videoinput',deviceId:'fake-cam-2',label:'Cam 2',groupId:'x',toJSON(){return {}}}]); }; })();"""

c, _ = api_login("admin")
mid, tok = new_live_meeting(c)
errors: list[str] = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=CHROME_ARGS)
    sctx = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"])
    sp = sctx.new_page()
    sp.on("pageerror", lambda e: errors.append(str(e)[:160]))
    browser_login(sp, "admin")
    sp.goto(f"{BASE}/projects/{PID}/meetings/{mid}/room")
    sp.wait_for_selector("#meeting-stage", timeout=20000)
    gctx = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"])
    gctx.add_init_script(STUB)
    gp = gctx.new_page()
    gp.on("pageerror", lambda e: errors.append(str(e)[:160]))
    gp.goto(f"{BASE}/m/{tok}")
    gp.fill("#guest-name", "Гость смоук")
    gp.click("#guest-join")
    gp.wait_for_selector("#meeting-stage", state="visible", timeout=25000)

    def tiles(page):
        return page.evaluate("document.querySelectorAll('#meeting-tiles .meeting-tile').length")

    check(bool(wait_until(lambda: tiles(sp) >= 2 and tiles(gp) >= 2)), "оба участника видят двоих")

    check(gp.evaluate("document.getElementById('meeting-cam-switch').hidden"), "смена камеры скрыта при выключенной камере")
    if gp.evaluate("!PrimaryAnaliticsMeeting.room.localParticipant.isCameraEnabled"):
        gp.click("#meeting-cam")
    check(
        bool(wait_until(lambda: not gp.evaluate("document.getElementById('meeting-cam-switch').hidden"), 8)),
        "кнопка смены камеры видна при 2+ камерах",
    )

    before = sp.evaluate("document.getElementById('meeting-room-status').textContent")
    c.post(f"/projects/{PID}/meetings/{mid}/end")

    def noticed():
        try:
            return sp.evaluate("(document.getElementById('meeting-room-status')||{}).textContent") != before or "/room" not in sp.url
        except Exception:
            return True  # страница ушла на другую

    check(bool(wait_until(noticed, 15)), "сотрудник замечает завершение встречи")
    b.close()
check(not errors, f"нет ошибок JS на страницах комнаты {errors[:2]}")
