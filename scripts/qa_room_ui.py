"""Комната созвона: раскладка на телефоне при повороте, пульт и окна, устойчивость индикаторов и камеры (живой LiveKit, песочница).

Дополняет qa_room.py (вход, камеры, микрофон, обрывы сети). Встречи создаются кодом приложения в песочнице (проект
SMOKE_PROJECT_ID, по умолчанию 29) и завершаются в конце; пароли не нужны. Проверяет:
  • телефон (профиль Pixel 7) вертикально и горизонтально, с показом экрана и без: все плитки видны целиком, не наезжают,
    экран в горизонтали — главное окно слева, участники — лентой справа;
  • пульт на ПК и телефоне: у каждой кнопки подсказка; микрофон, звук, камера, шумодав, «Спикер/Сетка» переключаются
    и возвращаются; «Участники» и меню динамика открываются и закрываются (Esc); «Скопировать ссылку»; «Развернуть окно»;
  • говорящий не шлёт setAttributes на каждый кадр (метка «микрофон молчит» — только при смене состояния);
  • камера, погашенная системой (наушники на телефоне, возврат из фона), возвращается сама; выключенная человеком — нет;
  • после переподключения участника у других нет устаревшего «нет связи».
Запуск: venv\\Scripts\\python.exe scripts\\qa_room_ui.py [--base https://127.0.0.1:777] [--shots папка]
Код выхода 0 — всё прошло. НЕ запускать во время настоящего созвона (проверяется)."""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from playwright.sync_api import sync_playwright  # noqa: E402
from qa_room import FAILS, check, end_meeting, join, make_meeting, wait_until  # noqa: E402

ARGS = ["--use-fake-device-for-media-stream=device-count=2", "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required", "--auto-select-desktop-capture-source=Entire screen"]
DESKTOP = {"viewport": {"width": 1280, "height": 800}}
MEASURE = """() => { const dock = document.getElementById('meeting-dock'); const vw = innerWidth; const d = dock ? dock.getBoundingClientRect() : {top: innerHeight};
  return Array.from(document.querySelectorAll('.meeting-tile')).map(t => { const r = t.getBoundingClientRect();
    return {screen: t.classList.contains('meeting-tile-screen'), w: r.width, h: r.height, top: r.top, bottom: r.bottom, left: r.left, right: r.right,
            inside: r.bottom <= d.top + 1 && r.top >= 0 && r.right <= vw + 1 && r.left >= 0}; }); }"""
LABELS = """() => Array.from(document.querySelectorAll('#meeting-dock button, #meeting-more-tray button, .meeting-tile-fs'))
  .filter(b => b.offsetParent !== null || b.closest('#meeting-more-tray'))
  .map(b => ({id: b.id || b.className, label: (b.getAttribute('aria-label') || b.title || b.textContent || '').trim()}))"""


def guest(browser, base: str, token: str, name: str, cam: bool = False, **ctx):
    kw = dict(DESKTOP)
    kw.update(ctx)
    page = browser.new_context(ignore_https_errors=True, permissions=["microphone", "camera", "clipboard-read", "clipboard-write"], **kw).new_page()
    page.on("pageerror", lambda e: JS_ERRORS.append(str(e)))
    join(page, base, token, name)
    wait_until(lambda: page.evaluate("!!(PrimaryAnaliticsMeeting.room && PrimaryAnaliticsMeeting.room.localParticipant)"), 10)
    if cam:
        page.click("#meeting-cam")
    return page


def overlaps(tiles) -> bool:
    for i, a in enumerate(tiles):
        for b in tiles[i + 1:]:
            if a["left"] < b["right"] - 1 and b["left"] < a["right"] - 1 and a["top"] < b["bottom"] - 1 and b["top"] < a["bottom"] - 1:
                return True
    return False


def phone_layout(browser, base: str, shots: Path | None) -> None:
    db, meeting, token = make_meeting()
    try:
        others = [guest(browser, base, token, f"Участник {i + 1}", cam=True) for i in range(3)]
        phone = guest(browser, base, token, "Телефон", cam=True, **dict(browser_devices()["Pixel 7"]))
        wait_until(lambda: phone.locator(".meeting-tile video").count() >= 4, 30)
        for step in ("без экрана", "с показом экрана"):
            if step == "с показом экрана":
                others[0].evaluate("PrimaryAnaliticsMeeting.room.localParticipant.setScreenShareEnabled(true, {contentHint: 'detail'}, {simulcast: false})")
                wait_until(lambda: phone.locator(".meeting-tile-screen video").count() >= 1, 20)
            for orient, size in (("вертикально", {"width": 412, "height": 839}), ("горизонтально", {"width": 839, "height": 412})):
                phone.set_viewport_size(size)
                time.sleep(2)
                tiles = phone.evaluate(MEASURE)
                if shots:
                    phone.screenshot(path=str(shots / f"phone_{step}_{orient}.png".replace(" ", "_")))
                tag = f"[телефон {orient}, {step}]"
                check(all(t["inside"] for t in tiles), f"{tag} все {len(tiles)} плиток видны целиком, над панелью")
                check(not overlaps(tiles), f"{tag} плитки не наезжают друг на друга")
                screen = [t for t in tiles if t["screen"]]
                if screen and orient == "горизонтально":
                    s = screen[0]
                    rest = [t for t in tiles if not t["screen"]]
                    check(s["h"] >= 0.7 * size["height"] and s["w"] >= 0.6 * size["width"], f"{tag} экран — главное окно ({s['w']:.0f}×{s['h']:.0f})")
                    check(all(t["left"] >= s["right"] - 1 for t in rest), f"{tag} участники — лентой справа от экрана")
    finally:
        end_meeting(db, meeting)


def dock(browser, base: str) -> None:
    db, meeting, token = make_meeting()
    try:
        guest(browser, base, token, "Собеседник")
        for tag, kw in (("ПК", {}), ("телефон", dict(browser_devices()["Pixel 7"]))):
            page = guest(browser, base, token, f"Проверка {tag}", **kw)
            wait_until(lambda: page.locator(".meeting-tile").count() >= 2, 20)
            labels = page.evaluate(LABELS)
            unnamed = [x["id"] for x in labels if not x["label"]]
            check(not unnamed, f"[{tag}] у всех {len(labels)} кнопок пульта есть подсказка (без подсказки: {unnamed})")
            state = lambda sel: page.evaluate(f"(() => {{ const e = document.querySelector('{sel}'); return e ? e.getAttribute('aria-pressed') + '|' + e.classList.contains('is-off') : null; }})()")  # noqa: E731
            shown = lambda sel: page.evaluate(f"(() => {{ const e = document.querySelector('{sel}'); return !!e && !e.hidden && e.offsetParent !== null; }})()")  # noqa: E731

            def more():
                if shown("#meeting-more") and not shown("#meeting-more-tray"):
                    page.click("#meeting-more")
                    time.sleep(0.3)

            for sel, name in (("#meeting-mic", "Микрофон"), ("#meeting-sound", "Звук"), ("#meeting-cam", "Камера"), ("#meeting-denoise", "Шумодав")):
                more()
                if not shown(sel):
                    continue
                before = state(sel)
                page.click(sel)
                time.sleep(1.0)
                mid = state(sel)
                more()
                page.click(sel)
                time.sleep(1.0)
                check(before != mid and state(sel) == before, f"[{tag}] «{name}» переключается и возвращается")
            more()
            if shown("#meeting-layout"):
                was = page.evaluate("PrimaryAnaliticsMeeting._layout")
                page.click("#meeting-layout")
                time.sleep(0.4)
                now = page.evaluate("PrimaryAnaliticsMeeting._layout")
                more()
                page.click("#meeting-layout")
                time.sleep(0.4)
                check(was != now and page.evaluate("PrimaryAnaliticsMeeting._layout") == was, f"[{tag}] «Спикер/Сетка» переключается и возвращается")
            more()
            if shown("#meeting-roster"):
                page.click("#meeting-roster")
                time.sleep(0.4)
                check(shown("#meeting-roster-panel") or shown(".meeting-roster-panel"), f"[{tag}] «Участники»: окно открылось")
                page.keyboard.press("Escape")
                time.sleep(0.4)
                check(not (shown("#meeting-roster-panel") or shown(".meeting-roster-panel")), f"[{tag}] «Участники»: закрывается по Esc")
            if shown("#meeting-speaker-btn"):
                page.click("#meeting-speaker-btn")
                time.sleep(0.4)
                check(shown("#meeting-speaker-menu"), f"[{tag}] меню динамика открывается")
                page.keyboard.press("Escape")
                time.sleep(0.3)
                if shown("#meeting-speaker-menu"):
                    page.mouse.click(5, 5)
                    time.sleep(0.3)
                check(not shown("#meeting-speaker-menu"), f"[{tag}] меню динамика закрывается")
            more()
            if shown("#meeting-copy"):
                page.click("#meeting-copy")
                check(bool(wait_until(lambda: page.locator(".meeting-toast").count() >= 1 and page.inner_text(".meeting-toast").strip(), 3)),
                      f"[{tag}] «Скопировать ссылку» показывает уведомление")
            fs = page.locator(".meeting-tile:not(.meeting-tile-local) .meeting-tile-fs").first
            if fs.count():
                fs.click()
                time.sleep(0.4)
                check(page.evaluate("document.querySelector('.meeting-tiles').classList.contains('has-focus') || !!document.fullscreenElement"),
                      f"[{tag}] «Развернуть окно» у плитки срабатывает")
                page.keyboard.press("Escape")
            page.context.close()
    finally:
        end_meeting(db, meeting)


def resilience(browser, base: str) -> None:
    db, meeting, token = make_meeting()
    try:
        a = guest(browser, base, token, "Гость А")
        bb = guest(browser, base, token, "Гость Б")
        a_id = a.evaluate("PrimaryAnaliticsMeeting.room.localParticipant.identity")
        a.evaluate("""() => { window.__setAttr = 0; const lp = PrimaryAnaliticsMeeting.room.localParticipant; const orig = lp.setAttributes.bind(lp);
          lp.setAttributes = (x) => { window.__setAttr += 1; return orig(x); }; }""")
        time.sleep(15)
        n = a.evaluate("window.__setAttr")
        check(n <= 2, f"говорящий не шлёт setAttributes на каждый кадр (за 15 с: {n})")
        cam_on = lambda: a.evaluate("PrimaryAnaliticsMeeting.room.localParticipant.isCameraEnabled")  # noqa: E731
        mute = "PrimaryAnaliticsMeeting.room.localParticipant.getTrackPublication('camera').track.mute()"
        a.click("#meeting-cam")
        check(bool(wait_until(lambda: bb.locator(".meeting-tile:not(.meeting-tile-local).meeting-tile-has-video").count() >= 1, 20)), "Б видит камеру А")
        a.evaluate(mute)  # так livekit-client глушит камеру, которую не смог перезапустить
        check(bool(wait_until(cam_on, 8)), "камера, погашенная системой, вернулась сама")
        a.evaluate(mute)
        a.evaluate("navigator.mediaDevices.dispatchEvent(new Event('devicechange'))")
        check(bool(wait_until(cam_on, 8)), "после смены устройств (наушники) камера вернулась")
        check(bool(wait_until(lambda: bb.locator(".meeting-tile:not(.meeting-tile-local).meeting-tile-cam-off").count() == 0, 10)),
              "у Б значок камеры А снова «включена»")
        a.click("#meeting-cam")
        time.sleep(6)
        check(cam_on() is False, "камера, выключенная человеком, сама не включается")
        bb.evaluate(f"PrimaryAnaliticsMeeting._quality['{a_id}'] = 'lost'")
        a.reload()
        a.wait_for_selector("#guest-name")
        if a.locator("#guest-join").is_visible():
            a.fill("#guest-name", "Гость А")
            a.click("#guest-join")
        a.wait_for_selector("#meeting-stage.is-open", timeout=30000)
        check(bool(wait_until(lambda: bb.evaluate(f"PrimaryAnaliticsMeeting._quality['{a_id}'] === undefined"), 20)),
              "после переподключения А у Б нет устаревшего «нет связи» для А")
    finally:
        end_meeting(db, meeting)


_DEVICES = {}
JS_ERRORS: list[str] = []


def browser_devices():
    return _DEVICES


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:777")
    ap.add_argument("--shots", default="")
    args = ap.parse_args()
    live = subprocess.run([sys.executable, str(ROOT / "scripts" / "live_meetings.py")], capture_output=True)
    if live.returncode == 3:
        print("Идёт настоящая встреча — проверку не запускаю.")
        return 3
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        _DEVICES.update(p.devices)
        browser = p.chromium.launch(headless=True, args=ARGS)
        phone_layout(browser, args.base, shots)
        dock(browser, args.base)
        resilience(browser, args.base)
        browser.close()
    check(not JS_ERRORS, f"ошибок JS нет ({JS_ERRORS[:3]})")
    print("\nИТОГ:", "ВСЁ ПРОШЛО" if not FAILS else f"ПРОВАЛЕНО {len(FAILS)}: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
