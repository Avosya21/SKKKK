"""Удаление встречи при подключённом госте (находка аудита 4.9): гость покидает комнату, запись не возобновляется."""
import os
import time

from playwright.sync_api import sync_playwright

from common import *

DATA = os.environ.get("SMOKE_DATA_DIR", r"D:\Primary_Analitics\data")
c, _ = api_login("admin")
mid, tok = new_live_meeting(c)
room = c.get(f"/projects/{PID}/meetings/{mid}").json()["room_name"]
tracks = os.path.join(DATA, "meetings", room, "tracks")


def size() -> int:
    return sum(os.path.getsize(os.path.join(tracks, f)) for f in os.listdir(tracks)) if os.path.isdir(tracks) else 0


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=CHROME_ARGS)
    pg = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"]).new_page()
    pg.goto(f"{BASE}/m/{tok}")
    pg.fill("#guest-name", "Гость")
    pg.click("#guest-join")
    pg.wait_for_selector("#meeting-stage", state="visible", timeout=25000)
    time.sleep(5)
    c.post(f"/projects/{PID}/meetings/{mid}/end")
    time.sleep(0.5)
    check(c.delete(f"/projects/{PID}/meetings/{mid}").status_code == 204, "встреча удалена")
    def left_room():
        try:  # после выхода страница перезагружается и получает 404 (встреча удалена): сцены нет вовсе
            return pg.evaluate("(function(){var s=document.getElementById('meeting-stage');return !s || s.hidden;})()")
        except Exception:
            return True  # идёт навигация

    check(bool(wait_until(left_room, 20)), "гость сам вышел из комнаты")
    time.sleep(3)
    s1 = size()
    time.sleep(6)
    check(size() - s1 < 150_000, f"запись не растёт после выхода гостя (было {s1} байт; живая запись дала бы ~600 КБ за 6 с)")
    b.close()
room_dir = os.path.join(DATA, "meetings", room)
gone = wait_until(lambda: not os.path.isdir(room_dir), 150, step=5)
if gone:
    print("PASS каталог записи удалённой встречи исчез с диска")
elif os.environ.get("SMOKE_STRICT_CLEANUP") == "1":
    check(False, "каталог записи удалённой встречи исчез с диска")
else:
    # Известная открытая проблема (находка 31): voice_cleaner.py держит файлы дорожек после закрытия комнаты.
    print(f"WARN каталог {room} остался на диске (файлы заняты voice_cleaner.py, находка 31); SMOKE_STRICT_CLEANUP=1 сделает это ошибкой")
