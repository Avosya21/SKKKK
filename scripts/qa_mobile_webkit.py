"""Проверка гостевого входа и комнаты на мобильных профилях: WebKit (движок Safari) для iPhone/iPad и Chromium для Android-профилей.

Это эмуляция экрана и user-agent: вёрстка, касания, JS-ошибки, поведение при отказе в камере и микрофоне. Настоящие камера, звук и iOS сюда не входят
(для них: scripts/qa_mobile_android.py с телефоном по USB и облачные устройства, см. docs/TESTING.md).

Запуск:  venv\\Scripts\\python.exe scripts\\qa_mobile_webkit.py [--base https://127.0.0.1:777] [--shots папка]
Код выхода 0 — всё прошло. Создаёт встречу в песочнице кодом приложения и завершает её в конце."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from playwright.sync_api import sync_playwright  # noqa: E402

from qa_room import check, end_meeting, make_meeting, wait_until, FAILS  # noqa: E402

PROFILES = [
    ("webkit", "iPhone 15"), ("webkit", "iPhone 15 landscape"), ("webkit", "iPad Pro 11"),
    ("chromium", "Pixel 7"), ("chromium", "Galaxy S9+"),
]
BENIGN = ("favicon", "ERR_INTERNET_DISCONNECTED", "Permission denied", "NotAllowedError", "NotFoundError")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:777")
    ap.add_argument("--shots", default="")
    args = ap.parse_args()
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    db, meeting, token = make_meeting()
    print(f"встреча #{meeting.id}", flush=True)
    try:
        with sync_playwright() as p:
            for engine, name in PROFILES:
                browser = getattr(p, engine).launch(headless=True)
                ctx = browser.new_context(**p.devices[name], ignore_https_errors=True)
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e, errs=errors: errs.append(str(e)))
                page.on("console", lambda m, errs=errors: errs.append(m.text) if m.type == "error" else None)
                tag = f"[{engine}/{name}]"
                page.goto(f"{args.base}/m/{token}")
                page.wait_for_selector("#guest-name", timeout=30000)
                vw = page.evaluate("innerWidth")
                check(page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), f"{tag} вход: нет горизонтального скролла (ширина {vw})")
                box = page.evaluate("(()=>{const r=document.getElementById('guest-join').getBoundingClientRect();return [r.top,r.bottom,innerHeight,r.height]})()")
                check(box[1] <= box[2] and box[3] >= 40, f"{tag} вход: кнопка «Войти» видна без прокрутки и не мельче 40 px (низ {box[1]:.0f} из {box[2]}, высота {box[3]:.0f})")
                if shots:
                    page.screenshot(path=str(shots / f"{name.replace(' ', '_')}_lobby.png"))
                page.fill("#guest-name", "Гость-" + name.split()[0])
                page.click("#guest-join")
                # без разрешений на камеру/микрофон: страница должна остаться живой и что-то сказать, а не «зависнуть»
                entered = wait_until(lambda: page.locator("#meeting-stage.is-open").count() > 0, 25)
                status = ""
                if entered:
                    time.sleep(3)
                    status = page.inner_text("#meeting-room-status") if page.locator("#meeting-room-status").count() else ""
                check(bool(entered), f"{tag} вход без разрешений на устройства: комната открылась")
                if entered:
                    check(page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), f"{tag} комната: нет горизонтального скролла")
                    dock = page.evaluate("(()=>{const d=document.getElementById('meeting-dock');if(!d)return null;const r=d.getBoundingClientRect();return [r.left,r.right,innerWidth,r.bottom,innerHeight]})()")
                    check(bool(dock) and dock[0] >= -1 and dock[1] <= dock[2] + 1 and dock[3] <= dock[4] + 1, f"{tag} комната: панель управления целиком на экране {dock}")
                    small = page.evaluate("Array.from(document.querySelectorAll('#meeting-dock button')).filter(b=>{const r=b.getBoundingClientRect();return r.width>0&&(r.width<36||r.height<36)}).map(b=>b.id)")
                    check(not small, f"{tag} комната: кнопки панели не меньше 36 px (мелкие: {small})")
                    check(page.locator("#meeting-leave").is_visible(), f"{tag} комната: кнопка «Выйти» видна")
                    if shots:
                        page.screenshot(path=str(shots / f"{name.replace(' ', '_')}_room.png"))
                real = [e for e in errors if not any(x in e for x in BENIGN)]
                check(not real, f"{tag} нет ошибок JS" + (f": {real[:2]}" if real else ""))
                ctx.close()
                browser.close()
    finally:
        end_meeting(db, meeting)
    print("\nИТОГ:", "ВСЁ ПРОШЛО" if not FAILS else f"ПРОВАЛЕНО {len(FAILS)}")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
