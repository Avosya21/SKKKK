"""Проверка комнаты на настоящем Chrome для Android: эмулятор Pixel 7 (Android 14, папка tools/android) или телефон по USB с отладкой.

Телефон и «компьютер» (Chromium с фейковыми устройствами) входят в одну встречу по публичной ссылке. Проверяется: вход, вёрстка, камеры
(зеркало у фронтальной, без зеркала у тыловой, переключение), динамик, Wake Lock, размер кнопок, обрыв сети и возврат в комнату.

Запуск:  venv\\Scripts\\python.exe scripts\\qa_mobile_android.py [--boot] [--base https://<публичный-адрес>:777] [--shots папка] [--outage 30]
  --boot     сам запустить эмулятор (нужны tools/android/sdk и созданный AVD pixel7; см. docs/TESTING.md)
Код выхода: 0 — всё прошло, 1 — есть провалы, 77 — нет устройства."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from playwright.sync_api import sync_playwright  # noqa: E402

from qa_room import FAILS, check, end_meeting, make_meeting, wait_until  # noqa: E402

ANDROID = ROOT / "tools" / "android"
ADB = str(ANDROID / "sdk" / "platform-tools" / "adb.exe")
if not Path(ADB).is_file():
    ADB = str(ANDROID / "platform-tools" / "adb.exe")
CHROME_FLAGS = "_ chrome --disable-fre --no-first-run --no-default-browser-check --ignore-certificate-errors --use-fake-ui-for-media-stream --disable-features=Translate"


def adb(*args: str, timeout: int = 60) -> str:
    r = subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


def device_ready() -> bool:
    return adb("shell", "getprop", "sys.boot_completed").strip() == "1"


def boot_emulator() -> None:
    env = {**os.environ, "JAVA_HOME": str(ANDROID / "jdk"), "ANDROID_SDK_ROOT": str(ANDROID / "sdk"), "ANDROID_HOME": str(ANDROID / "sdk"), "ANDROID_AVD_HOME": str(ANDROID / "avd")}
    subprocess.Popen([str(ANDROID / "sdk" / "emulator" / "emulator.exe"), "-avd", "pixel7", "-no-window", "-no-audio", "-no-snapshot", "-no-boot-anim",
                      "-gpu", "swiftshader_indirect", "-camera-back", "virtualscene", "-camera-front", "emulated"],
                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x00000008)
    for _ in range(80):
        if device_ready():
            return
        time.sleep(5)


def prepare_chrome() -> None:
    adb("root")
    time.sleep(2)
    adb("shell", f"echo '{CHROME_FLAGS}' > /data/local/tmp/chrome-command-line && chmod 755 /data/local/tmp/chrome-command-line")
    for perm in ("CAMERA", "RECORD_AUDIO"):
        adb("shell", "pm", "grant", "com.android.chrome", f"android.permission.{perm}")
    adb("shell", "am", "force-stop", "com.android.chrome")
    adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", "about:blank", "com.android.chrome")
    time.sleep(6)
    adb("forward", "tcp:9222", "localabstract:chrome_devtools_remote")


def set_network(on: bool) -> None:
    state = "enable" if on else "disable"
    adb("shell", "svc", "wifi", state)
    adb("shell", "svc", "data", state)


def _default_base() -> str:
    """Публичный адрес сервера из настроек (PRIMARY_ANALITICS_PUBLIC_HOST в .env): телефон ходит по нему, как настоящий гость."""
    try:
        from app.app.config import get_settings

        host = (get_settings().primary_analitics_public_host or "").strip()
    except Exception:  # noqa: BLE001
        host = ""
    return f"https://{host or '127.0.0.1'}:777"


def join_desktop(page, base: str, token: str, name: str) -> None:
    page.goto(f"{base}/m/{token}")
    page.wait_for_selector("#guest-name")
    page.fill("#guest-name", name)
    page.click("#guest-join")
    page.wait_for_selector("#meeting-stage.is-open", timeout=30000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=_default_base())
    ap.add_argument("--boot", action="store_true")
    ap.add_argument("--shots", default="")
    ap.add_argument("--outage", type=int, default=30)
    args = ap.parse_args()
    if not Path(ADB).is_file():
        print("SKIP: нет adb (tools/android). См. docs/TESTING.md")
        return 77
    adb("start-server")
    if not device_ready() and args.boot:
        boot_emulator()
    if not device_ready():
        print("SKIP: нет Android-устройства (запустите эмулятор или подключите телефон; --boot запускает эмулятор)")
        return 77
    prepare_chrome()
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    db, meeting, token = make_meeting()
    print(f"встреча #{meeting.id}; Android {adb('shell', 'getprop', 'ro.build.version.release')}, экран {adb('shell', 'wm', 'size')}", flush=True)
    errors: list[str] = []
    try:
        with sync_playwright() as p:
            desk_b = p.chromium.launch(headless=True, args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
            desk = desk_b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"]).new_page()
            phone_b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            phone = phone_b.contexts[0].new_page()
            phone.on("pageerror", lambda e: errors.append(str(e)))

            phone.goto(f"{args.base}/m/{token}", timeout=40000)
            phone.wait_for_selector("#guest-name", timeout=20000)
            info = phone.evaluate("[innerWidth, innerHeight, navigator.userAgent.includes('Android'), !!navigator.wakeLock, typeof HTMLMediaElement.prototype.setSinkId]")
            print(f"  телефон: экран {info[0]}x{info[1]} CSS px, Android={info[2]}, wakeLock={info[3]}, setSinkId={info[4]}", flush=True)
            check(phone.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "вход: нет горизонтального скролла")
            box = phone.evaluate("(()=>{const r=document.getElementById('guest-join').getBoundingClientRect();return [r.bottom, innerHeight, r.height]})()")
            check(box[0] <= box[1] and box[2] >= 40, f"вход: кнопка «Войти» видна без прокрутки, высота {box[2]:.0f} px")
            phone.fill("#guest-name", "Телефон")
            phone.click("#guest-join")
            phone.wait_for_selector("#meeting-stage.is-open", timeout=40000)
            joined = wait_until(lambda: "Вы в комнате" in phone.inner_text("#meeting-room-status"), 40)
            print("  статус:", phone.inner_text("#meeting-room-status")[:100], flush=True)
            check(bool(joined), "телефон вошёл в комнату")
            join_desktop(desk, args.base, token, "Компьютер")
            check(wait_until(lambda: phone.locator(".meeting-tile").count() >= 2, 30) is not None and phone.locator(".meeting-tile").count() >= 2, f"телефон видит второго участника (плиток {phone.locator('.meeting-tile').count()})")

            # --- камера: зеркало / переключение
            phone.click("#meeting-cam")
            got_video = wait_until(lambda: phone.locator(".meeting-tile-local .meeting-tile-stream").count() >= 1, 30)
            check(bool(got_video), "камера телефона включилась")
            if got_video:
                facing = phone.evaluate("(()=>{const t=PrimaryAnaliticsMeeting.room.localParticipant.getTrackPublication('camera');const s=t&&t.track&&t.track.mediaStreamTrack;return s?(s.getSettings().facingMode||'?')+' | '+s.label:'-'})()")
                print("  камера:", facing, flush=True)
                mirrored = phone.locator(".meeting-tile-local .meeting-tile-stream.is-mirror").count() == 1
                front = "environment" not in facing and not any(w in facing.lower() for w in ("back", "rear"))
                check(mirrored == front, f"зеркало соответствует камере (камера {'фронтальная' if front else 'тыловая'}, зеркало {'есть' if mirrored else 'нет'})")
                sw = phone.locator("#meeting-cam-switch")
                wait_until(sw.is_visible, 10)
                if sw.is_visible():
                    before = phone.locator(".meeting-tile-local .meeting-tile-stream.is-mirror").count()
                    sw.click()
                    cam_js = "(()=>{const t=PrimaryAnaliticsMeeting.room.localParticipant.getTrackPublication('camera');const s=t&&t.track&&t.track.mediaStreamTrack;return s?(s.getSettings().facingMode||'?')+' | '+s.label:'-'})()"
                    switched = wait_until(lambda: phone.evaluate(cam_js) != facing and "back" in phone.evaluate(cam_js).lower(), 25)
                    after_facing = phone.evaluate(cam_js)
                    print("  после переключения:", after_facing, flush=True)
                    check(bool(switched), "камера реально переключилась на тыловую")
                    time.sleep(1)
                    after = phone.locator(".meeting-tile-local .meeting-tile-stream.is-mirror").count()
                    check(after == 0, f"тыловая камера: своё изображение НЕ зеркалится (зеркало {'есть' if after else 'нет'})")
                    if shots:
                        phone.screenshot(path=str(shots / "android_room_back_camera.png"))
                else:
                    print("  SKIP переключение камеры: кнопка скрыта (камер меньше двух)", flush=True)
            if shots:
                phone.screenshot(path=str(shots / "android_room.png"))

            # --- микрофон, динамик, Wake Lock, кнопки
            mic = phone.evaluate("PrimaryAnaliticsMeeting.room.localParticipant.isMicrophoneEnabled")
            print(f"  микрофон включён: {mic} (в эмуляторе без звука это нормально: печатается для сведения)", flush=True)
            wrap = phone.locator("#meeting-speaker-wrap")
            phone.click("#meeting-more") if phone.locator("#meeting-more").is_visible() else None
            check(wrap.count() == 1 and wrap.get_attribute("hidden") is None, "кнопка динамика видна на телефоне")
            wl = phone.evaluate("!!PrimaryAnaliticsMeeting._wakeLock")
            check(wl or not info[3], f"экран телефона удерживается включённым (Wake Lock {'активен' if wl else 'нет'})")
            small = phone.evaluate("Array.from(document.querySelectorAll('#meeting-dock button')).filter(b=>{const r=b.getBoundingClientRect();return r.width>0&&(r.width<36||r.height<36)}).map(b=>b.id)")
            check(not small, f"кнопки панели не меньше 36 px (мелкие: {small})")
            check(phone.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "комната: нет горизонтального скролла")

            # --- обрыв сети телефона
            print(f"  отключаю сеть телефона на {args.outage} с…", flush=True)
            set_network(False)
            time.sleep(args.outage)
            set_network(True)
            back = wait_until(lambda: phone.evaluate("PrimaryAnaliticsMeeting.room && PrimaryAnaliticsMeeting.room.state") == "connected", 120)
            check(bool(back), f"после обрыва сети на {args.outage} с телефон снова в комнате")
            check(wait_until(lambda: desk.locator(".meeting-tile").count() >= 2, 30) is not None and desk.locator(".meeting-tile").count() >= 2, "компьютер по-прежнему видит телефон")
            real = [e for e in errors if "ERR_INTERNET_DISCONNECTED" not in e and "favicon" not in e]
            check(not real, "ошибок JS на телефоне нет" + (f": {real[:2]}" if real else ""))
            phone.close()
            desk_b.close()
    finally:
        set_network(True)
        end_meeting(db, meeting)
    print("\nИТОГ:", "ВСЁ ПРОШЛО" if not FAILS else f"ПРОВАЛЕНО {len(FAILS)}")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
