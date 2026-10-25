"""Проверка комнаты созвона в настоящем браузере (Chromium + фейковые камера/микрофон), без паролей: встреча создаётся кодом приложения
в песочнице (проект SMOKE_PROJECT_ID, по умолчанию 29), входят два гостя по публичной ссылке.

Проверяет: вход и плитки; зеркало только у фронтальной камеры; выбор микрофона и динамика в комнате; возврат в комнату после
кратковременного и длительного обрыва сети (место участника сохраняется); отсутствие ошибок JS.

Запуск (нужен venv с playwright):  venv\\Scripts\\python.exe scripts\\qa_room.py [--base https://127.0.0.1:777]
Код выхода 0 — всё прошло. Встреча в конце завершается."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, message: str) -> None:
    print(("PASS " if cond else "FAIL ") + message, flush=True)
    if not cond:
        FAILS.append(message)


def wait_until(fn, timeout: float = 20.0, step: float = 0.4):
    end = time.time() + timeout
    while time.time() < end:
        try:
            value = fn()
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return value
        time.sleep(step)
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def make_meeting():
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services import meetings as ms

    db = SessionLocal()
    pid = int(os.environ.get("SMOKE_PROJECT_ID", "29"))
    project = db.get(Project, pid)
    admin = db.query(User).filter(User.role == "admin").first()
    meeting, token = ms.create_meeting(db, get_settings(), project=project, actor=admin)
    ms.start_meeting(db, meeting)
    return db, meeting, token


def end_meeting(db, meeting):
    from app.app.services import meetings as ms

    try:
        ms.end_meeting(db, meeting)
    finally:
        db.close()


def join(page, base: str, token: str, name: str):
    page.goto(f"{base}/m/{token}")
    page.wait_for_selector("#guest-name")
    page.fill("#guest-name", name)
    page.click("#guest-join")
    page.wait_for_selector("#meeting-stage.is-open", timeout=30000)
    wait_until(lambda: "Вы в комнате" in page.inner_text("#meeting-room-status"), 30)


def tiles(page) -> int:
    return page.locator(".meeting-tile").count()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:777")
    ap.add_argument("--outage", type=int, default=45, help="длительность второго (долгого) обрыва сети, с; 170 проверяет собственный цикл возврата")
    args = ap.parse_args()
    db, meeting, token = make_meeting()
    print(f"встреча #{meeting.id}, комната {meeting.room_name}", flush=True)
    errors: list[str] = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=[
                "--use-fake-device-for-media-stream=device-count=3",
                "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required",
            ])
            ctx_a = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"], viewport={"width": 1280, "height": 800})
            ctx_b = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"], viewport={"width": 1280, "height": 800})
            # Подмена facingMode у видеотреков: window.__facing = 'user' | 'environment' (имитация фронтальной и тыловой камеры)
            ctx_a.add_init_script("""
              window.__facing = 'user';
              const orig = MediaStreamTrack.prototype.getSettings;
              MediaStreamTrack.prototype.getSettings = function () {
                const s = orig.call(this);
                if (this.kind === 'video') s.facingMode = window.__facing;
                return s;
              };
            """)
            a, bb = ctx_a.new_page(), ctx_b.new_page()
            for pg in (a, bb):
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "favicon" not in m.text else None)
            join(a, args.base, token, "Гость А")
            join(bb, args.base, token, "Гость Б")
            check(wait_until(lambda: tiles(a) >= 2, 25) is True or tiles(a) >= 2, f"А видит обоих участников (плиток: {tiles(a)})")
            check(tiles(bb) >= 2, f"Б видит обоих участников (плиток: {tiles(bb)})")

            # --- камера: включить, зеркало по фронтальной/тыловой
            a.click("#meeting-cam")
            wait_until(lambda: a.locator(".meeting-tile-local .meeting-tile-stream").count() >= 1, 20)
            check(a.locator(".meeting-tile-local .meeting-tile-stream.is-mirror").count() == 1, "фронтальная камера: своё изображение зеркалится")
            a.evaluate("window.__facing = 'environment'; PrimaryAnaliticsMeeting._syncLocalMirror()")
            check(a.locator(".meeting-tile-local .meeting-tile-stream.is-mirror").count() == 0, "тыловая камера: зеркала нет")
            a.evaluate("window.__facing = 'user'; PrimaryAnaliticsMeeting._syncLocalMirror()")
            check(a.locator(".meeting-tile-local .meeting-tile-stream.is-mirror").count() == 1, "снова фронтальная: зеркало вернулось")
            check(bb.locator(".meeting-tile-stream.is-mirror").count() == 0, "у других участников чужое видео не зеркалится")

            # --- микрофон: выбор устройства в комнате
            if a.locator("#meeting-more").is_visible():  # на узком экране элементы спрятаны в «Ещё»
                a.click("#meeting-more")
            wait_until(lambda: a.locator("#meeting-mic-pick-wrap").count() == 1, 10)
            check(a.locator("#meeting-mic-pick-wrap").count() == 1, "в комнате есть выбор микрофона")
            mics = a.evaluate("navigator.mediaDevices.enumerateDevices().then(d => d.filter(x => x.kind === 'audioinput').length)")
            print(f"  микрофонов в браузере: {mics}", flush=True)
            if mics >= 2:
                a.click("#meeting-mic-pick-btn")
                items = a.locator("#meeting-mic-pick-menu .meeting-speaker-item")
                check(items.count() >= 2, f"меню микрофонов содержит устройства ({items.count()})")
                before = a.evaluate("PrimaryAnaliticsMeeting._currentMicId()")
                target = items.nth(1 if items.nth(0).get_attribute("aria-current") == "true" else 0)
                target.click()
                changed = wait_until(lambda: a.evaluate("PrimaryAnaliticsMeeting._currentMicId()") != before, 12)
                check(bool(changed), "микрофон переключён в комнате (устройство трека сменилось)")
                check(a.evaluate("PrimaryAnaliticsMeeting.room.localParticipant.isMicrophoneEnabled") is True, "после смены микрофон остался включённым")
            else:
                print("  SKIP выбор микрофона: в браузере одно устройство", flush=True)

            # --- динамик
            wrap = a.locator("#meeting-speaker-wrap")
            check(wrap.count() == 1 and wrap.get_attribute("hidden") is None, "кнопка выбора динамика видна")

            # --- индикатор связи «палочки»: 4 зелёные отлично, 3 хорошо, 1 красная плохо, 0 нет связи; без мигания, статус не затирается
            # Настоящие оценки LiveKit в том же браузере перебивали проверочные (на этой машине камера гостя оценивается
            # «good/poor» попеременно), и проверка возврата к 4 палочкам падала не из-за кода. На время проверок в индикатор
            # попадают только события сценария; после — всё как было.
            a.evaluate("""() => { const m = PrimaryAnaliticsMeeting; m.__qaLinkQuality = m._linkQuality; window.__qaEmit = false;
              m._linkQuality = function (q) { if (window.__qaEmit) return m.__qaLinkQuality.call(m, q); }; }""")
            emit = ("(q) => { const LK = window.LivekitClient; const r = PrimaryAnaliticsMeeting.room; window.__qaEmit = true;"
                    " try { r.emit(LK.RoomEvent.ConnectionQualityChanged, q, r.localParticipant); } finally { window.__qaEmit = false; } }")
            level = lambda: a.evaluate("(document.getElementById('meeting-signal')||{dataset:{}}).dataset.level")  # noqa: E731
            status_before = a.evaluate("document.querySelector('#meeting-room-status').textContent")
            check(a.locator("#meeting-signal i").count() == 4, "индикатор связи из четырёх палочек есть в комнате")
            a.evaluate(emit, "excellent"); time.sleep(0.3)
            check(level() == "4", f"отличная связь: 4 палочки (сейчас {level()})")
            a.evaluate(emit, "good"); time.sleep(0.3)
            check(level() == "3", f"хорошая связь: 3 палочки (сейчас {level()})")
            a.evaluate(emit, "poor")
            seen = set()
            for step in range(14):  # 4,2 с наблюдения: провал 1 с, дальше связь снова хорошая (настоящие события LiveKit тоже могут прийти)
                if step == 3:
                    a.evaluate(emit, "good")
                seen.add(level())
                time.sleep(0.3)
            check("1" not in seen and "0" not in seen, f"краткий провал связи (1 с) палочки не дёргает (были состояния {sorted(seen)})")
            a.evaluate(emit, "poor")
            check(wait_until(lambda: level() == "1", 5) is not None and level() == "1", f"устойчиво плохая связь: одна палочка (сейчас {level()})")
            check(a.evaluate("document.querySelector('#meeting-room-status').textContent") == status_before, "строка статуса не затёрта")
            a.evaluate(emit, "lost")
            check(level() == "0", f"полный обрыв: ни одной палочки, сразу (сейчас {level()})")
            a.evaluate(emit, "excellent"); time.sleep(1.5)
            check(level() == "0", "связь на миг улучшилась: индикатор держится (задержка возврата)")
            check(wait_until(lambda: level() == "4", 6) is not None and level() == "4", f"связь стабильно отличная: снова 4 палочки (сейчас {level()})")
            title = a.evaluate("document.getElementById('meeting-signal').title")
            check("отличное" in title, f"подсказка по наведению словами: «{title}»")
            a.evaluate("() => { PrimaryAnaliticsMeeting._linkQuality = PrimaryAnaliticsMeeting.__qaLinkQuality; }")

            # --- единообразие: у каждого участника на плитке те же палочки
            remote = "(q) => { const LK = window.LivekitClient; const r = PrimaryAnaliticsMeeting.room; const p = Array.from(r.remoteParticipants.values()).find((x) => !String(x.identity).startsWith('primaryanalitics-')); r.emit(LK.RoomEvent.ConnectionQualityChanged, q, p); }"
            a.evaluate(remote, "poor"); time.sleep(0.6)
            tile_levels = a.evaluate("Array.from(document.querySelectorAll('.meeting-tile:not(.meeting-tile-local) .meeting-signal.is-tile')).map(x => x.dataset.level)")
            check(tile_levels == ["1"], f"чужая плитка: плохая связь показана одной красной палочкой (сейчас {tile_levels})")
            check(a.locator(".meeting-tile-local .meeting-signal.is-tile").count() == 1, "своя плитка тоже с палочками")
            check(a.locator(".meeting-chip.is-quality").count() == 0, "старых текстовых меток качества нет")

            # --- короткий обрыв сети (5 с): библиотека восстанавливает сама
            ctx_a.set_offline(True)
            time.sleep(5)
            ctx_a.set_offline(False)
            ok = wait_until(lambda: a.evaluate("PrimaryAnaliticsMeeting.room && PrimaryAnaliticsMeeting.room.state") == "connected", 40)
            check(ok, "после обрыва сети на 5 с гость А в комнате")
            check(wait_until(lambda: tiles(bb) >= 2, 20) is not None and tiles(bb) >= 2, "Б всё ещё видит А")

            # --- длительный обрыв (45 с): библиотека сдаётся, наш цикл возвращает в комнату
            a.click("#meeting-mic") if not a.evaluate("PrimaryAnaliticsMeeting.room.localParticipant.isMicrophoneEnabled") else None
            ctx_a.set_offline(True)
            seen: list[str] = []
            for _ in range(max(1, args.outage // 10)):
                time.sleep(10)
                st = a.evaluate("PrimaryAnaliticsMeeting.room.state")
                txt = a.evaluate("document.querySelector('#meeting-room-status').textContent").strip()[:60]
                seen.append(f"{st}|{txt}")
            print(f"  за {args.outage} с офлайна у А: " + " -> ".join(dict.fromkeys(seen)), flush=True)
            ctx_a.set_offline(False)
            back = wait_until(lambda: a.evaluate("PrimaryAnaliticsMeeting.room && PrimaryAnaliticsMeeting.room.state") == "connected", 90)
            check(bool(back), f"после длительного обрыва ({args.outage} с) гость А сам вернулся в комнату")
            check(wait_until(lambda: tiles(bb) >= 2 and tiles(a) >= 2, 30) is not None, "оба снова видят друг друга после возврата")
            mic_after = a.evaluate("PrimaryAnaliticsMeeting.room.localParticipant.isMicrophoneEnabled")
            check(mic_after is True, "микрофон после возврата снова включён")
            check(a.evaluate("document.querySelector('#meeting-room-status').textContent").strip() != "", "статус отображается")

            # --- ошибки страницы
            benign = ("favicon", "ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED")  # последние возникают от намеренного отключения сети
            real = [e for e in errors if not any(x in e for x in benign)]
            check(not real, "ошибок JS в консоли нет" + (f": {real[:3]}" if real else ""))
            b.close()
    finally:
        end_meeting(db, meeting)
    print("\nИТОГ:", "ВСЁ ПРОШЛО" if not FAILS else f"ПРОВАЛЕНО {len(FAILS)}: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
