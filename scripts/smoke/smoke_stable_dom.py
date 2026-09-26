"""Автообновление не перестраивает DOM без изменений (пункт 3.7): узел кнопки встречи живёт дольше двух опросов."""
import time

from playwright.sync_api import sync_playwright

from common import *

c, _ = api_login("admin")
mid, tok = new_live_meeting(c)
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=CHROME_ARGS)
    pg = b.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900}).new_page()
    browser_login(pg, "admin")
    pg.goto(f"{BASE}/projects/{PID}")
    pg.wait_for_selector("#meetings-body", state="attached")
    pg.evaluate("document.getElementById('meetings-panel').classList.add('show')")
    wait_until(lambda: pg.locator(f"#meetings-body .meeting-end[data-id='{mid}']").count() == 1, 10)
    sel = f"#meetings-body .meeting-end[data-id='{mid}']"
    verdict = None
    for attempt in range(4):
        # Список законно перерисовывается, если данные изменились (другие встречи песочницы, идущий анализ).
        # Баг — перерисовка при НЕИЗМЕННЫХ данных: сравниваем подпись ответа до и после.
        wait_until(lambda: pg.locator(sel).count() == 1, 10)
        pg.evaluate(f"window.__node = document.querySelector(\"{sel}\"); window.__sig = lastMeetingsSig")
        time.sleep(7.5)  # больше двух циклов опроса по 3 с
        same_node = pg.evaluate(f"window.__node === document.querySelector(\"{sel}\") && document.contains(window.__node)")
        same_data = pg.evaluate("window.__sig === lastMeetingsSig")
        if same_data:
            verdict = same_node
            break
        print(f"попытка {attempt + 1}: данные списка встреч изменились сами, повтор")
    check(verdict is True, "при неизменных данных кнопка «Завершить» осталась тем же DOM-узлом после нескольких опросов")
    b.close()
c.post(f"/projects/{PID}/meetings/{mid}/end")
