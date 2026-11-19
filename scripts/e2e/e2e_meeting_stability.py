"""E2E устойчивости комнаты созвона (живой стенд, песочница).

Шаги s1–s6 из docs/plans/MEETING-ROOM-STABILITY-PLAN.md.
Запуск (живой стенд, песочница, пароль не нужен):
  venv\\Scripts\\python.exe scripts\\e2e\\e2e_meeting_stability.py [--only s1]
Код выхода 0 — нет FAIL. Встречи в конце завершаются.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "smoke"))

from playwright.sync_api import sync_playwright  # noqa: E402

from common import BASE, CHROME_ARGS, PID  # noqa: E402
from qa_room import end_meeting, make_meeting  # noqa: E402

ARGS = CHROME_ARGS + [
    "--use-fake-device-for-media-stream=device-count=2",
    "--auto-select-desktop-capture-source=Entire screen",
]
FAILS: list[str] = []


def check(cond: bool, message: str) -> None:
    print(("PASS " if cond else "FAIL ") + message, flush=True)
    if not cond:
        FAILS.append(message)


def wait_until(fn, timeout: float = 15.0, step: float = 0.5):
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


def _check(cond: bool, message: str) -> None:
    check(bool(cond), message)


def _guest(browser, token: str, name: str):
    page = browser.new_context(
        ignore_https_errors=True,
        permissions=["microphone", "camera"],
        viewport={"width": 1280, "height": 800},
    ).new_page()
    page.goto(f"{BASE}/m/{token}")
    page.wait_for_selector("#guest-name", timeout=20000)
    page.fill("#guest-name", name)
    company = page.locator("#guest-company-select")
    if company.count() and company.is_visible():
        options = company.locator("option").all()
        for opt in options:
            value = opt.get_attribute("value") or ""
            if value and value != "__other__":
                company.select_option(value)
                break
    page.click("#guest-join")
    page.wait_for_selector("#meeting-stage.is-open", timeout=30000)
    _check(
        bool(wait_until(lambda: (page.inner_text("#meeting-people") or "").startswith("В комнате"), 25)),
        f"{name}: в комнате",
    )
    return page


def _ready(page) -> bool:
    try:
        return bool(page.evaluate("!!(PrimaryAnaliticsMeeting.room && PrimaryAnaliticsMeeting.room.localParticipant)"))
    except Exception:  # noqa: BLE001
        return False


def _rejoin(page) -> None:
    page.evaluate("() => PrimaryAnaliticsMeeting.room.disconnect()")
    page.wait_for_function("() => PrimaryAnaliticsMeeting._joined === false", timeout=10000)
    page.evaluate("() => PrimaryAnaliticsMeeting._beginRejoin(PrimaryAnaliticsMeeting._roomEls)")
    page.wait_for_function(
        "() => PrimaryAnaliticsMeeting._joined === true && PrimaryAnaliticsMeeting._rejoining === false",
        timeout=40000,
    )
    _check((page.inner_text("#meeting-people") or "").startswith("В комнате"), "перезаход вернул в комнату")


def scenario_s1(browser, token: str) -> None:
    page = _guest(browser, token, "Намерение")
    _check(bool(wait_until(lambda: _ready(page), 15)), "s1: сессия есть")
    page.evaluate("() => PrimaryAnaliticsMeeting.room.localParticipant.getTrackPublication('microphone').track.mute()")
    time.sleep(1)
    state = page.evaluate(
        """() => ({
          wanted: PrimaryAnaliticsMeeting._micWanted,
          pressed: document.getElementById('meeting-mic').getAttribute('aria-pressed')
        })"""
    )
    _check(state["wanted"] is True and state["pressed"] == "true", "s1: системный mute микрофона не снимает намерение и кнопку")
    page.click("#meeting-cam")
    _check(bool(wait_until(lambda: page.evaluate("PrimaryAnaliticsMeeting._camWanted === true"), 10)), "s1: камера включена намерением")
    page.click("#meeting-screen")
    _check(
        bool(wait_until(lambda: page.evaluate("PrimaryAnaliticsMeeting._screenWanted === true"), 15)),
        "s1: экран включён намерением",
    )
    page.click("#meeting-mic")
    _check(page.evaluate("PrimaryAnaliticsMeeting._micWanted === false"), "s1: кнопка микрофона выключает намерение")
    _rejoin(page)
    restored = page.evaluate(
        """() => ({
          mic: PrimaryAnaliticsMeeting.room.localParticipant.isMicrophoneEnabled,
          cam: PrimaryAnaliticsMeeting.room.localParticipant.isCameraEnabled,
          screen: PrimaryAnaliticsMeeting.room.localParticipant.isScreenShareEnabled,
          micWanted: PrimaryAnaliticsMeeting._micWanted,
          camWanted: PrimaryAnaliticsMeeting._camWanted,
          screenWanted: PrimaryAnaliticsMeeting._screenWanted,
          toast: (document.getElementById('meeting-toast') || {}).textContent || ''
        })"""
    )
    _check(restored["mic"] is False and restored["micWanted"] is False, "s1: выключенный микрофон после перезахода остаётся выключенным")
    _check(restored["cam"] is True and restored["camWanted"] is True, f"s1: камера вернулась по намерению {restored}")
    _check(restored["screen"] is True and restored["screenWanted"] is True, f"s1: экран вернулся по намерению {restored}")
    page.evaluate(
        """() => {
          const pub = PrimaryAnaliticsMeeting.room.localParticipant.getTrackPublication('screen_share');
          const media = pub && pub.track && pub.track.mediaStreamTrack;
          if (!media) return;
          media.stop();
          media.dispatchEvent(new Event('ended'));
        }"""
    )
    stopped = wait_until(
        lambda: page.evaluate(
            """() => PrimaryAnaliticsMeeting._screenWanted === false
              && document.getElementById('meeting-screen').getAttribute('aria-pressed') === 'false'"""
        )
        is True,
        8,
    )
    snap = page.evaluate(
        """() => ({
          wanted: PrimaryAnaliticsMeeting._screenWanted,
          pressed: (document.getElementById('meeting-screen')||{}).getAttribute && document.getElementById('meeting-screen').getAttribute('aria-pressed'),
          sharing: PrimaryAnaliticsMeeting.room && PrimaryAnaliticsMeeting.room.localParticipant.isScreenShareEnabled,
          state: PrimaryAnaliticsMeeting.room && PrimaryAnaliticsMeeting.room.state
        })"""
    )
    _check(bool(stopped), f"s1: плашка браузера «остановить показ» гасит кнопку экрана {snap}")
    page.context.close()


def scenario_s2(browser, token: str) -> None:
    guest = _guest(browser, token, "Голос")
    listener = _guest(browser, token, "Слушатель")
    _check(
        bool(wait_until(lambda: listener.locator(".meeting-tile:not(.meeting-tile-local)").count() >= 1, 20)),
        "s2: слушатель видит гостя",
    )
    listener.click("#meeting-sound")
    _check(bool(wait_until(lambda: listener.evaluate("PrimaryAnaliticsMeeting._soundOn === false"), 5)), "s2: звук выключен")

    def remote_sub():
        return listener.evaluate(
            """() => {
              let found = false, on = false;
              PrimaryAnaliticsMeeting.room.remoteParticipants.forEach(p => {
                p.trackPublications.forEach(pub => {
                  if (pub.kind !== 'audio') return;
                  const name = String(pub.trackName || pub.name || '');
                  if (name.indexOf('primaryanalitics-raw') >= 0) return;
                  const src = String(pub.source || '');
                  if (src.indexOf('screen') >= 0 || src === '4') return;
                  found = true; on = Boolean(pub.isSubscribed);
                });
              });
              return found ? on : null;
            }"""
        )

    _check(bool(wait_until(lambda: remote_sub() is False, 10)), "s2: без звука и без записи чужой микрофон отписан")
    listener.evaluate("() => { PrimaryAnaliticsMeeting._localRecordHoldSubs = true; PrimaryAnaliticsMeeting._syncRemoteAudioSubs(); }")
    _check(bool(wait_until(lambda: remote_sub() is True, 10)), "s2: удержание записи подписывает чужой микрофон")
    playing = listener.evaluate("() => document.querySelectorAll('#meeting-tiles audio').length")
    _check(playing == 0, "s2: при выключенном звуке чужой голос не играет в динамик")
    mix = listener.evaluate("() => PrimaryAnaliticsMeeting._localRecordMixTracks().length")
    _check(mix >= 2, f"s2: в микс записи входят свой и чужой микрофон (дорожек: {mix})")
    listener.evaluate("() => PrimaryAnaliticsMeeting._releaseLocalRecordSubs()")
    _check(bool(wait_until(lambda: remote_sub() is False, 10)), "s2: после отпускания записи подписка снова выключена")
    guest.context.close()
    listener.context.close()


def scenario_s3(browser, db, meeting, token: str) -> None:
    page = _guest(browser, token, "Камера")
    page.evaluate(
        """() => {
          const lp = PrimaryAnaliticsMeeting.room.localParticipant;
          lp.setCameraEnabled = () => Promise.reject(new Error('cam-fail'));
        }"""
    )
    page.click("#meeting-cam")
    status = page.inner_text("#meeting-room-status")
    _check("Камера" in status and not status.startswith("Микрофон"), f"s3: ошибка камеры подписана «Камера» ({status!r})")
    page.evaluate(
        """() => {
          window.__recordStops = 0;
          const orig = PrimaryAnaliticsMeeting._stopLocalRecorder.bind(PrimaryAnaliticsMeeting);
          PrimaryAnaliticsMeeting._stopLocalRecorder = (...args) => { window.__recordStops += 1; return orig(...args); };
          PrimaryAnaliticsMeeting._localRecorder = { isActive: () => true };
          const LK = window.LivekitClient;
          PrimaryAnaliticsMeeting.room.emit(LK.RoomEvent.Reconnecting);
        }"""
    )
    time.sleep(1)
    still = page.evaluate(
        """() => ({
          stops: window.__recordStops,
          active: !!(PrimaryAnaliticsMeeting._localRecorder && PrimaryAnaliticsMeeting._localRecorder.isActive()),
          rejoining: PrimaryAnaliticsMeeting._rejoining,
          status: document.getElementById('meeting-room-status').textContent,
          link: document.getElementById('meeting-link-state').textContent
        })"""
    )
    _check(still["stops"] == 0 and still["active"] is True and still["rejoining"] is False, "s3: короткое восстановление связи запись не обрывает")
    _check(
        "Связь пропала" in still["link"] and "Связь пропала" not in still["status"] and "Возвращаюсь" not in still["status"],
        f"s3: обрыв связи пишется отдельно от статуса комнаты ({still})",
    )
    from app.app.services import meetings as ms

    ms.end_meeting(db, meeting)
    _check(bool(wait_until(lambda: "Комната закрыта" in page.inner_text("#meeting-room-status"), 20)), "s3: закрытая комната показывает «Комната закрыта»")
    time.sleep(2)
    text = page.inner_text("#meeting-room-status") + " " + page.inner_text("#meeting-link-state")
    _check("Возвращаюсь" not in text and page.evaluate("PrimaryAnaliticsMeeting._rejoining === false"), "s3: закрытая комната не уходит в цикл перезахода")
    page.context.close()


def scenario_s4(browser, token: str) -> None:
    page = _guest(browser, token, "Связь")
    page.click("#meeting-cam")
    _check(bool(wait_until(lambda: page.evaluate("PrimaryAnaliticsMeeting.room.localParticipant.isCameraEnabled"), 10)), "s4: камера включена")
    page.evaluate("() => PrimaryAnaliticsMeeting._applySignal(1)")
    page.evaluate("() => PrimaryAnaliticsMeeting._scheduleCamRevive()")
    timers = page.evaluate("() => (PrimaryAnaliticsMeeting._camReviveTimers || []).length")
    _check(timers == 0, "s4: на слабой связи таймеры оживления камеры не ставятся")
    page.evaluate("() => PrimaryAnaliticsMeeting.room.localParticipant.getTrackPublication('camera').track.mute()")
    time.sleep(5)
    _check(page.evaluate("PrimaryAnaliticsMeeting.room.localParticipant.isCameraEnabled") is False, "s4: на слабой связи камера сама не включается")
    page.evaluate("() => PrimaryAnaliticsMeeting._applySignal(4)")
    _check(bool(wait_until(lambda: page.evaluate("PrimaryAnaliticsMeeting.room.localParticipant.isCameraEnabled"), 12)), "s4: на обычной связи системный mute камеры оживает")
    page.click("#meeting-screen")
    _check(bool(wait_until(lambda: page.evaluate("PrimaryAnaliticsMeeting.room.localParticipant.isScreenShareEnabled"), 15)), "s4: экран включён")
    page.evaluate("() => PrimaryAnaliticsMeeting._applySignal(1)")
    screen = page.evaluate(
        """() => ({
          on: PrimaryAnaliticsMeeting.room.localParticipant.isScreenShareEnabled,
          fps: PrimaryAnaliticsMeeting._screenFps
        })"""
    )
    _check(screen["on"] is True and screen["fps"] == 2, f"s4: экран остаётся, частота снижена ({screen})")
    page.context.close()


def scenario_s5(browser, token: str) -> None:
    page = _guest(browser, token, "Индикаторы")
    other = _guest(browser, token, "Второй")
    chips = page.evaluate(
        """() => ['meeting-link-state','meeting-publish-state','meeting-hear-state'].map(id => {
          const el = document.getElementById(id); return el ? el.textContent : '';
        })"""
    )
    _check(all(chips), f"s5: три индикатора на месте ({chips})")
    page.click("#meeting-cam")
    status = page.inner_text("#meeting-room-status")
    for banned in ("Вы в комнате", "Связь:", "Мои дорожки", "Слышу людей"):
        _check(banned not in status, f"s5: в углу нет «{banned}» ({status!r})")
    _check(page.locator("#meeting-state").is_hidden(), "s5: связь, дорожки и «слышу» скрыты")
    _check(
        bool(wait_until(lambda: page.evaluate("PrimaryAnaliticsMeeting.room.localParticipant.attributes.send_video === '1'"), 10)),
        "s5: намерение камеры ушло атрибутом",
    )
    held = page.evaluate(
        """() => {
          const L = PrimaryAnaliticsLayout;
          return L.flagState(null, {send_audio:'1'}, 'send_audio') === L.FLAG_ON
            && L.speakerHold('a', 9000, 10000, 'b', 2000) === 'a'
            && L.speakerHold('a', 7000, 10000, 'b', 2000) === 'b';
        }"""
    )
    _check(held is True, "s5: чип без публикации читает атрибут, говорящий держится 2 с")
    page.click("#meeting-layout")
    tile = page.locator(".meeting-tile").first
    tile.locator("[data-tile-fs]").click()
    _check(page.evaluate("PrimaryAnaliticsMeeting._pinLocked === true"), "s5: ручной pin сильнее смены говорящего")
    other.context.close()
    page.context.close()


def scenario_s6(browser, db, meeting, token: str) -> None:
    page = _guest(browser, token, "Устройства")
    calls = page.evaluate(
        """() => {
          window.__camCalls = 0;
          const lp = PrimaryAnaliticsMeeting.room.localParticipant;
          const orig = lp.setCameraEnabled.bind(lp);
          lp.setCameraEnabled = (...args) => { window.__camCalls += 1; return orig(...args); };
          navigator.mediaDevices.dispatchEvent(new Event('devicechange'));
          return true;
        }"""
    )
    _check(calls is True, "s6: событие смены устройств отправлено")
    time.sleep(2)
    after = page.evaluate(
        """() => ({
          cams: window.__camCalls,
          state: PrimaryAnaliticsMeeting.room.state
        })"""
    )
    _check(after["cams"] == 0 and after["state"] == "connected", f"s6: смена аудиоустройств не перезапускает камеру и не рвёт комнату ({after})")
    page.context.close()

    guest = _guest(browser, token, "Пульс")

    def listed() -> bool:
        from app.app.db import SessionLocal
        from app.app.services.meetings import ongoing_meetings

        fresh = SessionLocal()
        try:
            return any(row.id == meeting.id for row, _people in ongoing_meetings(fresh, meeting.project_id))
        finally:
            fresh.close()

    _check(bool(wait_until(listed, 15)), "s6: пульс держит полоску «Идёт созвон»")
    time.sleep(25)
    _check(listed(), "s6: через 25 с живой участник всё ещё на полоске")
    guest.context.close()
    deadline = time.time() + 85
    gone = False
    while time.time() < deadline:
        if not listed():
            gone = True
            break
        time.sleep(3)
    _check(gone, "s6: закрытая вкладка снимает полоску примерно за минуту")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="s1,s2,… или один шаг")
    args = parser.parse_args()
    want = {item.strip() for item in args.only.split(",") if item.strip()}
    print(f"BASE={BASE} PID={PID}")
    scenarios = ["s1", "s2", "s3", "s4", "s5", "s6"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=ARGS)
        try:
            for name in scenarios:
                if want and name not in want:
                    continue
                db, meeting, token = make_meeting()
                print(f"=== {name} meeting={meeting.id}")
                try:
                    if name == "s1":
                        scenario_s1(browser, token)
                    elif name == "s2":
                        scenario_s2(browser, token)
                    elif name == "s3":
                        scenario_s3(browser, db, meeting, token)
                    elif name == "s4":
                        scenario_s4(browser, token)
                    elif name == "s5":
                        scenario_s5(browser, token)
                    elif name == "s6":
                        scenario_s6(browser, db, meeting, token)
                finally:
                    try:
                        end_meeting(db, meeting)
                    except Exception as err:  # noqa: BLE001
                        print(f"WARN end {meeting.id}: {err}")
        finally:
            browser.close()
    if FAILS:
        print(f"FAIL {len(FAILS)}")
        return 1
    print("PASS meeting stability")
    return 0


if __name__ == "__main__":
    sys.exit(main())
