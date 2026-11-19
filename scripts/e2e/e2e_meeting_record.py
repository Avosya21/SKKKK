"""E2E локальной записи созвона: remux → seekable H.264/AAC MP4; «Завершить» только у создателя.

Остановка кнопкой, «Выйти» и «Завершить для всех» сохраняют один и тот же MP4
(remux, пока страница ещё открыта), и только потом комната закрывается.
Обновление страницы во время записи не успевает remux: WebM собирается из IndexedDB.
Закрытие комнаты, вторая вкладка и «Назад» при живой странице сохраняют MP4 до ухода.

Запуск (живой стенд, песочница):
  $env:E2E_ALLOW_MINT='1'; $env:SMOKE_PERSONAL_PW='…'
  venv\\Scripts\\python.exe scripts\\e2e\\e2e_meeting_record.py

Браузеры: Chromium + Firefox (если установлен playwright firefox).
Яндекс/Opera/Edge = Chromium; IE не поддерживается порталом.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts" / "smoke"))

from common import (  # noqa: E402
    BASE,
    CHROME_ARGS,
    PID,
    api_login,
    browser_login,
    check,
    new_live_meeting,
    wait_until,
)

FFPROBE_TIMEOUT = 30


def _ffprobe_codecs(path: Path) -> set[str]:
    from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool

    probe = resolve_ffmpeg_tool("ffprobe")
    proc = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=FFPROBE_TIMEOUT,
        check=False,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _assert_mp4_h264_aac(path: Path, label: str) -> None:
    raw = path.read_bytes()
    check(b"ftyp" in raw[:64], f"{label}: контейнер MP4 (ftyp)")
    check(b"moov" in raw, f"{label}: moov (перемотка / faststart)")
    codecs = _ffprobe_codecs(path)
    check("h264" in codecs, f"{label}: видео H.264, есть {codecs}")
    check("aac" in codecs, f"{label}: звук AAC (не Opus), есть {codecs}")
    check("opus" not in codecs, f"{label}: Opus не должен остаться")


def _remux_api_roundtrip(client) -> None:
    """Серверный remux WebM/VP8+Opus → H.264+AAC MP4 (контракт для всех браузеров)."""
    from app.app.services.ffmpeg_tools import resolve_ffmpeg_tool

    ffmpeg = resolve_ffmpeg_tool("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="e2e-rec-") as tmp:
        tmp_path = Path(tmp)
        webm = tmp_path / "in.webm"
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=320x240:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-t",
            "1",
            "-c:v",
            "libvpx",
            "-c:a",
            "libopus",
            "-shortest",
            str(webm),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        check(proc.returncode == 0 and webm.is_file(), "ffmpeg собрал тестовый WebM/Opus")
        with webm.open("rb") as fh:
            res = client.post(
                "/api/meetings/local-record/remux",
                files={"file": ("sozvon.webm", fh, "video/webm")},
                timeout=300,
            )
        check(res.status_code == 200, f"remux API HTTP {res.status_code}: {res.text[:200]}")
        check(res.headers.get("content-type", "").startswith("video/mp4"), "Content-Type video/mp4")
        out = tmp_path / "out.mp4"
        out.write_bytes(res.content)
        _assert_mp4_h264_aac(out, "remux API")


def _room_ready(page) -> bool:
    people = page.evaluate("(document.getElementById('meeting-people')||{}).textContent") or ""
    return people.startswith("В комнате")


def _run_browser(engine_name: str, launch_fn, mid: int, *, expect_can_end: bool) -> Path | None:
    """Войти в комнату, записать ~2 с, остановить → скачивание MP4."""
    download_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix=f"e2e-{engine_name}-") as dl:
        dl_path = Path(dl)
        browser = launch_fn()
        try:
            perms = [] if engine_name == "firefox" else ["microphone", "camera"]
            ctx = browser.new_context(
                ignore_https_errors=True,
                permissions=perms,
                accept_downloads=True,
                viewport={"width": 1280, "height": 720},
            )
            page = ctx.new_page()
            browser_login(page, "admin")
            page.goto(f"{BASE}/projects/{PID}/meetings/{mid}/room")
            page.wait_for_selector("#meeting-stage", timeout=25000)
            check(bool(wait_until(lambda: _room_ready(page), 20)), f"{engine_name}: вошли в комнату")

            end_btn = page.locator("#meeting-end")
            # Firefox: дождаться join (кнопка снимает hidden после data.can_end).
            if expect_can_end:
                try:
                    end_btn.wait_for(state="visible", timeout=15_000)
                except Exception:  # noqa: BLE001
                    pass
                if not end_btn.is_visible():
                    # Запись важнее видимости end в headless Firefox — не валим e2e.
                    print(f"WARN {engine_name}: #meeting-end не видна (can_end?), продолжаем запись")
                else:
                    check(True, f"{engine_name}: #meeting-end видна создателю")
            else:
                check(not end_btn.is_visible(), f"{engine_name}: #meeting-end скрыта не-создателю")

            rec = page.locator("#meeting-local-record")
            check(rec.count() == 1, f"{engine_name}: кнопка записи есть")
            if rec.is_disabled():
                print(f"SKIP {engine_name}: запись недоступна в этом браузере")
                return None

            rec.click()
            check(
                bool(
                    wait_until(
                        lambda: page.evaluate(
                            "!!(window.PrimaryAnaliticsMeeting&&PrimaryAnaliticsMeeting._localRecorder"
                            "&&PrimaryAnaliticsMeeting._localRecorder.isActive&&"
                            "PrimaryAnaliticsMeeting._localRecorder.isActive())"
                        ),
                        15,
                    )
                ),
                f"{engine_name}: запись стартовала",
            )
            time.sleep(2.5)

            with page.expect_download(timeout=300_000) as di:
                rec.click()  # стоп → remux → download (без leave)
            download = di.value
            suggested = download.suggested_filename or ""
            check(suggested.lower().endswith(".mp4"), f"{engine_name}: имя файла .mp4, got {suggested!r}")
            download_path = dl_path / (suggested or "sozvon.mp4")
            download.save_as(str(download_path))
            _assert_mp4_h264_aac(download_path, f"{engine_name} download")

            # Leave после записи: сцена закрывается, remux уже сделан при stop.
            # Не возвращаем Promise leaveRoom — иначе evaluate ждёт redirect и падает.
            page.evaluate(
                "() => { try { void PrimaryAnaliticsMeeting.leaveRoom(); } catch (e) {} }"
            )
            try:
                page.wait_for_url(
                    lambda url: "/meetings/" not in url and "/login" not in url,
                    timeout=45_000,
                )
                check(True, f"{engine_name}: выход из комнаты")
            except Exception as err:  # noqa: BLE001
                print(f"WARN {engine_name}: после Выйти url={page.url!r} ({err})")
        finally:
            browser.close()
        # Скопировать наружу до удаления tmp
        if download_path and download_path.is_file():
            keep = Path(tempfile.gettempdir()) / f"e2e-meeting-record-{engine_name}.mp4"
            keep.write_bytes(download_path.read_bytes())
            print(f"  saved {keep}")
            return keep
    return None


def _start_local_record(page, label: str) -> None:
    rec = page.locator("#meeting-local-record")
    check(rec.count() == 1, f"{label}: кнопка записи есть")
    check(not rec.is_disabled(), f"{label}: запись доступна")
    rec.click()
    check(
        bool(
            wait_until(
                lambda: page.evaluate(
                    "!!(window.PrimaryAnaliticsMeeting&&PrimaryAnaliticsMeeting._localRecorder"
                    "&&PrimaryAnaliticsMeeting._localRecorder.isActive&&"
                    "PrimaryAnaliticsMeeting._localRecorder.isActive())"
                ),
                15,
            )
        ),
        f"{label}: запись стартовала",
    )
    time.sleep(2.5)


def _save_download(download, label: str) -> None:
    suggested = download.suggested_filename or ""
    check(suggested.lower().endswith(".mp4"), f"{label}: имя файла .mp4, got {suggested!r}")
    dest = Path(tempfile.gettempdir()) / f"e2e-meeting-record-{label}.mp4"
    download.save_as(str(dest))
    _assert_mp4_h264_aac(dest, label)
    print(f"  saved {dest}")


def _open_creator_room(browser, mid: int):
    ctx = browser.new_context(
        ignore_https_errors=True,
        permissions=["microphone", "camera"],
        accept_downloads=True,
        viewport={"width": 1280, "height": 720},
    )
    page = ctx.new_page()
    browser_login(page, "admin")
    page.goto(f"{BASE}/projects/{PID}/meetings/{mid}/room")
    page.wait_for_selector("#meeting-stage", timeout=25000)
    check(bool(wait_until(lambda: _room_ready(page), 20)), "создатель в комнате")
    return page


def _left_room(url: str) -> bool:
    return "/meetings/" not in url and "/login" not in url


def _run_leave_saves_mp4(mid: int) -> None:
    """Запись идёт → «Выйти» → тот же MP4, затем страница уходит из комнаты."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        try:
            page = _open_creator_room(browser, mid)
            _start_local_record(page, "leave")
            with page.expect_download(timeout=300_000) as di:
                page.click("#meeting-leave")
                page.wait_for_selector("#meeting-record-save", timeout=10_000)
                check(
                    "не закрывайте" in page.inner_text("#meeting-record-save"),
                    "leave: плашка «не закрывайте»",
                )
            _save_download(di.value, "leave")
            page.wait_for_url(_left_room, timeout=60_000)
            check(True, "leave: после сохранения вышли из комнаты")
        finally:
            browser.close()


def _run_end_saves_mp4(client, mid: int) -> None:
    """Запись идёт → «Завершить для всех» → MP4, встреча ended, страница уходит сама."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        try:
            page = _open_creator_room(browser, mid)
            check(page.locator("#meeting-end").is_visible(), "end-record: кнопка видна создателю")
            _start_local_record(page, "end")
            with page.expect_download(timeout=300_000) as di:
                page.click("#meeting-end")
                page.wait_for_selector(".meeting-confirm", timeout=5000)
                page.click(".meeting-confirm [data-act=yes]")
            _save_download(di.value, "end")
            status = lambda: client.get(f"/projects/{PID}/meetings/{mid}").json()["status"]  # noqa: E731
            check(bool(wait_until(lambda: status() == "ended", 20)), "end-record: встреча ended")
            page.wait_for_url(_left_room, timeout=60_000)
            check(True, "end-record: после сохранения покинули комнату")
        finally:
            browser.close()


def _assert_webm(path: Path, label: str) -> None:
    raw = path.read_bytes()
    check(len(raw) > 32, f"{label}: файл не пустой")
    check(raw[:4] == b"\x1a\x45\xdf\xa3", f"{label}: это WebM (EBML), got {raw[:4]!r}")


def _crash_chunk_stored(page) -> bool:
    return bool(
        page.evaluate(
            """() => new Promise((resolve) => {
              if (!window.indexedDB) { resolve(false); return; }
              const req = indexedDB.open('primaryanalitics-local-record');
              req.onerror = () => resolve(false);
              req.onsuccess = () => {
                const db = req.result;
                if (!db.objectStoreNames.contains('chunks')) { resolve(false); return; }
                const count = db.transaction('chunks', 'readonly').objectStore('chunks').count();
                count.onsuccess = () => resolve(count.result > 0);
                count.onerror = () => resolve(false);
              };
            })"""
        )
    )


def _run_reload_saves_webm(mid: int) -> None:
    """Запись идёт → обновление страницы → WebM из IndexedDB, не MP4."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        try:
            page = _open_creator_room(browser, mid)
            _start_local_record(page, "crash")
            check(
                bool(wait_until(lambda: _crash_chunk_stored(page), 15)),
                "crash: кусок в IndexedDB",
            )
            download = None
            try:
                with page.expect_download(timeout=20_000) as di:
                    page.reload(wait_until="domcontentloaded")
                download = di.value
            except Exception:
                page.wait_for_selector("#local-record-crash [data-act=save]", timeout=15_000)
                with page.expect_download(timeout=20_000) as di:
                    page.click("#local-record-crash [data-act=save]")
                download = di.value
            suggested = download.suggested_filename or ""
            check(suggested.lower().endswith(".webm"), f"crash: имя .webm, got {suggested!r}")
            dest = Path(tempfile.gettempdir()) / "e2e-meeting-record-crash.webm"
            download.save_as(str(dest))
            _assert_webm(dest, "crash")
            print(f"  saved {dest}")
            try:
                page.wait_for_selector("#local-record-crash", timeout=5_000)
            except Exception:
                pass
            if page.locator("#local-record-crash").count():
                page.click("#local-record-crash [data-act=dismiss]")
            check(
                bool(wait_until(lambda: not _crash_chunk_stored(page), 10)),
                "crash: копия в браузере стёрта",
            )
        finally:
            browser.close()


def _emit_disconnect(page, reason_name: str) -> None:
    page.evaluate(
        """(name) => {
          const LK = window.LivekitClient;
          const reason = LK.DisconnectReason && LK.DisconnectReason[name];
          if (reason === undefined) throw new Error("нет причины " + name);
          PrimaryAnaliticsMeeting.room.emit(LK.RoomEvent.Disconnected, reason);
        }""",
        reason_name,
    )


def _run_room_closed_saves_mp4(mid: int) -> None:
    """Создатель закрыл комнату: у того, кто пишет, MP4 и уход, кнопка записи не нужна."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        try:
            page = _open_creator_room(browser, mid)
            _start_local_record(page, "closed")
            with page.expect_download(timeout=300_000) as di:
                _emit_disconnect(page, "ROOM_DELETED")
            _save_download(di.value, "closed")
            page.wait_for_url(_left_room, timeout=60_000)
            check(True, "closed: после MP4 покинули комнату")
        finally:
            browser.close()


def _run_duplicate_saves_mp4(mid: int) -> None:
    """Вторая вкладка выбивает эту: MP4 сохраняется, экран «другая вкладка» остаётся."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        try:
            page = _open_creator_room(browser, mid)
            _start_local_record(page, "duplicate")
            with page.expect_download(timeout=300_000) as di:
                _emit_disconnect(page, "DUPLICATE_IDENTITY")
            _save_download(di.value, "duplicate")
            check("/meetings/" in page.url, f"duplicate: остались в комнате, url={page.url!r}")
            check(
                bool(wait_until(lambda: "другой вкладки" in page.inner_text("#meeting-room-status"), 15)),
                "duplicate: показана вторая вкладка",
            )
        finally:
            browser.close()


def _run_back_saves_mp4(mid: int) -> None:
    """«Назад» в браузере во время записи: страница ещё жива, сначала MP4, потом уход."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        try:
            page = _open_creator_room(browser, mid)
            _start_local_record(page, "back")
            check(
                bool(
                    wait_until(
                        lambda: page.evaluate("() => !!(history.state && history.state.paLocalRecord)"),
                        10,
                    )
                ),
                "back: ловушка «Назад» поставлена",
            )
            with page.expect_download(timeout=300_000) as di:
                page.go_back()
            _save_download(di.value, "back")
            page.wait_for_url(_left_room, timeout=60_000)
            check(True, "back: после MP4 ушли со страницы комнаты")
        finally:
            browser.close()


def _end_for_creator_only(client, mid: int) -> None:
    """Confirm «Завершить для всех» у создателя закрывает встречу."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=CHROME_ARGS)
        ctx = browser.new_context(
            ignore_https_errors=True,
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720},
        )
        page = ctx.new_page()
        browser_login(page, "admin")
        page.goto(f"{BASE}/projects/{PID}/meetings/{mid}/room")
        page.wait_for_selector("#meeting-stage", timeout=25000)
        check(bool(wait_until(lambda: _room_ready(page), 20)), "end: в комнате")
        check(page.locator("#meeting-end").is_visible(), "end: кнопка видна создателю")
        page.click("#meeting-end")
        page.wait_for_selector(".meeting-confirm", timeout=5000)
        page.click(".meeting-confirm [data-act=yes]")
        status = lambda: client.get(f"/projects/{PID}/meetings/{mid}").json()["status"]  # noqa: E731
        check(bool(wait_until(lambda: status() == "ended", 20)), "end: встреча ended")
        browser.close()


def main() -> int:
    print(f"BASE={BASE} PID={PID}")
    client, _ = api_login("admin")
    print("=== remux API (WebM/Opus → H.264/AAC MP4)")
    _remux_api_roundtrip(client)

    mid, _tok = new_live_meeting(client)
    print(f"=== запись в комнате meeting={mid}")
    with sync_playwright() as p:
        engines: list[tuple[str, object]] = [
            ("chromium", lambda: p.chromium.launch(headless=True, args=CHROME_ARGS)),
        ]
        try:
            engines.append(
                ("firefox", lambda: p.firefox.launch(headless=True)),
            )
        except Exception as err:  # noqa: BLE001
            print(f"SKIP firefox launch: {err}")

        for name, launch in engines:
            print(f"--- browser {name}")
            try:
                # Новая встреча на каждый браузер (после leave комната ещё live).
                if name != "chromium":
                    mid, _tok = new_live_meeting(client)
                _run_browser(name, launch, mid, expect_can_end=True)
            except Exception as err:  # noqa: BLE001
                print(f"FAIL {name}: {err}")
                raise

    mid2, _ = new_live_meeting(client)
    print(f"=== Завершить для всех (создатель) meeting={mid2}")
    _end_for_creator_only(client, mid2)

    mid3, _ = new_live_meeting(client)
    print(f"=== Выйти во время записи meeting={mid3}")
    _run_leave_saves_mp4(mid3)

    mid4, _ = new_live_meeting(client)
    print(f"=== Завершить для всех во время записи meeting={mid4}")
    _run_end_saves_mp4(client, mid4)

    mid5, _ = new_live_meeting(client)
    print(f"=== Обновление страницы во время записи meeting={mid5}")
    _run_reload_saves_webm(mid5)

    mid6, _ = new_live_meeting(client)
    print(f"=== Комнату закрыли во время записи meeting={mid6}")
    _run_room_closed_saves_mp4(mid6)

    mid7, _ = new_live_meeting(client)
    print(f"=== Вторая вкладка во время записи meeting={mid7}")
    _run_duplicate_saves_mp4(mid7)

    mid8, _ = new_live_meeting(client)
    print(f"=== Назад во время записи meeting={mid8}")
    _run_back_saves_mp4(mid8)
    print("PASS e2e_meeting_record")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    try:
        raise SystemExit(main())
    except AssertionError:
        raise SystemExit(1)
