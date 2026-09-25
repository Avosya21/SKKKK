"""Планшетные ширины и базовая доступность (пункты 4.2, 4.8): нет горизонтального скролла; у кнопок/ссылок/полей есть имя."""
import time

from playwright.sync_api import sync_playwright

from common import *

WIDTHS = [(768, 1024), (1024, 768), (1280, 800)]
NAME_JS = """
() => {
  const bad = [];
  const visible = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  for (const e of document.querySelectorAll('button, a[href], input:not([type=hidden]), select, textarea')) {
    if (!visible(e)) continue;
    const name = (e.getAttribute('aria-label') || e.getAttribute('title') || e.textContent || e.value || e.placeholder || '').trim()
      || (e.id && document.querySelector('label[for="' + e.id + '"]') ? 'label' : '') || (e.closest('label') ? 'label' : '')
      || (e.getAttribute('aria-labelledby') ? 'labelledby' : '');
    if (!name) bad.push(e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + '.' + String(e.className).split(' ')[0]);
  }
  return bad.slice(0, 6);
}
"""
c, _ = api_login("admin")
mid, tok = new_live_meeting(c)
issues = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=CHROME_ARGS)
    staff = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"]).new_page()
    browser_login(staff, "admin")
    guest = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"]).new_page()
    for w, h in WIDTHS:
        for page, url, label in ((staff, "/", "список"), (staff, f"/projects/{PID}", "проект"), (guest, f"/m/{tok}", "гость")):
            page.set_viewport_size({"width": w, "height": h})
            page.goto(BASE + url)
            time.sleep(2.5 if label == "проект" else 1.2)
            over = page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
            if over:
                issues.append(f"{label} {w}x{h}: горизонтальный скролл")
            print(("FAIL " if over else "PASS ") + f"{label} {w}x{h}: без горизонтального скролла" if not over else f"FAIL {label} {w}x{h}: горизонтальный скролл")
    for page, url, label in ((staff, "/", "список"), (staff, f"/projects/{PID}", "проект"), (guest, f"/m/{tok}", "гость"),
                             (staff, f"/projects/{PID}/meetings/{mid}/room", "комната")):
        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto(BASE + url)
        time.sleep(3)
        bad = page.evaluate(NAME_JS)
        print(("PASS " if not bad else "FAIL ") + f"a11y {label}: элементов без имени {bad}")
        if bad:
            issues.append(f"a11y {label}: {bad}")
    b.close()
c.post(f"/projects/{PID}/meetings/{mid}/end")
check(not issues, f"нет проблем ширины и доступности {issues}")
