"""Согласованность счётчиков проекта (пункт 3.16): Док. + Созвоны = знаменатель «Готово» = число строк таблицы = ответ API."""
import re
import time

from playwright.sync_api import sync_playwright

from common import *

c, _ = api_login("admin")
api_total = len(c.get(f"/projects/{PID}/artifacts").json())
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=CHROME_ARGS)
    pg = b.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900}).new_page()
    browser_login(pg, "admin")
    pg.goto(f"{BASE}/projects/{PID}")
    time.sleep(4)
    txt = lambda sel: pg.evaluate(f"(document.querySelector('{sel}')||{{}}).textContent") or ""  # noqa: E731
    done = txt("#kpi-done"); docs = int(re.sub(r"\D", "", txt("#kpi-docs")) or 0); calls = int(re.sub(r"\D", "", txt("#kpi-calls")) or 0)
    rows = pg.evaluate("document.querySelectorAll('#artifacts-table tbody tr').length")
    total = int(done.split("/")[1]) if "/" in done else -1
    check(docs + calls == total, f"Док. {docs} + Созвоны {calls} = знаменатель «Готово» {total}")
    check(total == rows == api_total, f"знаменатель {total} = строк таблицы {rows} = ответ API {api_total}")
    b.close()
