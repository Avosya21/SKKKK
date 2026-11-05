"""Жизненный цикл встречи на живом сервере: создание, запуск, ссылка, вход и выход, завершение, удаление (в том числе без запуска
и вместе с проектом). Действия — кодом приложения и публичным API (как кнопки портала), участники — настоящий Chromium с
фейковыми устройствами; после каждого шага сверяются база, каталог записи и комнаты LiveKit.

Работает в песочнице (проект SMOKE_PROJECT_ID, по умолчанию 29) и во временном проекте, который сам создаёт и удаляет.
Пароли не нужны. НЕ запускать во время настоящего созвона (проверяется).
Запуск: venv\\Scripts\\python.exe scripts\\qa_meeting_lifecycle.py [--base https://127.0.0.1:777] [--linger 150]
  --linger  сколько секунд ждать после удаления, что запись не вернулась на диск (по умолчанию 150: дольше повторных очисток)
Код выхода 0 — всё прошло."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from playwright.sync_api import sync_playwright  # noqa: E402
from qa_room import FAILS, check, join, wait_until  # noqa: E402

ARGS = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"]


def app():
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.meeting import Meeting, MeetingParticipant
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services import meetings as ms
    from app.app.services import project_purge as pp

    return get_settings(), SessionLocal, Meeting, MeetingParticipant, Project, User, ms, pp


def rooms() -> list[str]:
    from livekit import api

    s = app()[0]

    async def _list():
        client = api.LiveKitAPI(s.livekit_rtc_internal, s.livekit_api_key, s.livekit_api_secret)
        try:
            return [r.name for r in (await client.room.list_rooms(api.ListRoomsRequest())).rooms]
        finally:
            await client.aclose()

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, _list()).result(timeout=20)


def public(base: str, token: str, path: str = "", body: dict | None = None):
    url = f"{base}/api/meetings/public/{token}{path}"
    r = httpx.post(url, json=body, verify=False, timeout=15) if body is not None else httpx.get(url, verify=False, timeout=15)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") and r.content else None)


def room_dir(room: str) -> Path:
    return app()[0].data_dir / "meetings" / room


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:777")
    ap.add_argument("--linger", type=int, default=150)
    args = ap.parse_args()
    if subprocess.run([sys.executable, str(ROOT / "scripts" / "live_meetings.py")], capture_output=True).returncode == 3:
        print("Идёт настоящая встреча — проверку не запускаю.")
        return 3
    settings, SessionLocal, Meeting, MeetingParticipant, Project, User, ms, pp = app()
    db = SessionLocal()
    admin = db.query(User).filter(User.role == "admin").first()
    sandbox = db.get(Project, int(os.environ.get("SMOKE_PROJECT_ID", "29")))
    base = args.base
    to_check_later: list[str] = []

    # 1. создана, не запущена
    m1, t1 = ms.create_meeting(db, settings, project=sandbox, actor=admin)
    print(f"встреча #{m1.id} (без запуска), комната {m1.room_name}", flush=True)
    code, payload = public(base, t1)
    check(code == 200 and payload and payload.get("status") == "created" and not payload.get("started"), f"гостевая ссылка отвечает: встреча создана, не начата ({code}, {payload})")
    check(payload and payload.get("project_id") is None, "гостю не раскрывается номер проекта")
    code, _ = public(base, t1, "/join", {"display_name": "Гость", "guest_id": uuid.uuid4().hex})
    check(code == 409, f"войти до запуска нельзя (код {code})")
    check(m1.room_name not in rooms(), "комната в LiveKit до запуска не создана")
    try:
        ms.end_meeting(db, m1)
        check(False, "завершить не начатую встречу нельзя")
    except Exception as exc:  # noqa: BLE001
        check("409" in str(getattr(exc, "status_code", "")) or "not started" in str(exc).lower(), "завершить не начатую встречу нельзя (409)")
    # 2. удаление без запуска
    mid, room1 = m1.id, m1.room_name
    pp.purge_meeting(db, settings, m1)
    db.expire_all()
    check(db.get(Meeting, mid) is None, "удаление без запуска: встречи нет в базе")
    check(not room_dir(room1).exists(), "удаление без запуска: каталога записи нет")
    code, _ = public(base, t1)
    check(code == 404, f"ссылка удалённой встречи не работает (код {code})")

    # 3. полный цикл
    m2, t2 = ms.create_meeting(db, settings, project=sandbox, actor=admin)
    ms.start_meeting(db, m2)
    print(f"встреча #{m2.id} (полный цикл), комната {m2.room_name}", flush=True)
    code, payload = public(base, t2)
    check(code == 200 and payload.get("started"), "после запуска гостевая ссылка: встреча идёт")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=ARGS)
        ga = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"]).new_page()
        gb = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"]).new_page()
        join(ga, base, t2, "Гость А")
        join(gb, base, t2, "Гость Б")
        check(bool(wait_until(lambda: ga.locator(".meeting-tile").count() >= 2, 20)), "оба гостя в комнате")
        check(m2.room_name in rooms(), "комната в LiveKit создана")
        people = lambda: db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == m2.id).all()  # noqa: E731
        db.expire_all()
        check(len(people()) == 2, f"в базе два участника ({len(people())})")
        tracks = lambda: sorted(f.name for f in (room_dir(m2.room_name) / "tracks").glob("guest-*.wav")) if (room_dir(m2.room_name) / "tracks").is_dir() else []  # noqa: E731
        check(bool(wait_until(lambda: len(tracks()) >= 2, 25)), f"записываются дорожки обоих гостей ({tracks()})")
        # повторный вход той же вкладкой (тот же guest_id) — тот же участник, без дубля
        ga.reload()
        ga.wait_for_selector("#guest-name")
        if ga.locator("#guest-join").is_visible():
            ga.fill("#guest-name", "Гость А")
            ga.click("#guest-join")
        ga.wait_for_selector("#meeting-stage.is-open", timeout=30000)
        db.expire_all()
        check(len(people()) == 2, f"повторный вход с тем же guest_id не плодит участников ({len(people())})")
        # пульс и выход публичным API
        a_id = wait_until(lambda: ga.evaluate("PrimaryAnaliticsMeeting.room && PrimaryAnaliticsMeeting.room.localParticipant && PrimaryAnaliticsMeeting.room.localParticipant.identity"), 15)
        code, _ = public(base, t2, "/heartbeat", {"identity": a_id})
        check(code == 204, f"пульс гостя принимается ({code})")
        code, _ = public(base, t2, "/heartbeat", {"identity": "member:1"})
        check(code == 403, f"по гостевой ссылке нельзя слать пульс за сотрудника ({code})")
        # отзыв ссылки: гости по ней выведены из комнаты, встреча продолжается; новая ссылка работает, старая — нет
        ms.revoke_link(db, m2)
        code, _ = public(base, t2)
        check(code == 404, f"отозванная ссылка не работает ({code})")
        check(bool(wait_until(lambda: "/m/" in ga.url and ga.locator("#meeting-stage.is-open").count() == 0, 15)),
              "гость по отозванной ссылке выведен из комнаты")
        check(m2.room_name in rooms(), "после отзыва ссылки встреча продолжается (комната открыта)")
        _m, t2b = ms.regenerate_link(db, m2)
        code, payload = public(base, t2b)
        check(code == 200 and payload.get("started"), "новая ссылка работает")
        code, _ = public(base, t2)
        check(code == 404, "старая ссылка после новой не работает")
        gc = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"]).new_page()
        join(gc, base, t2b, "Гость В")
        check(gc.locator("#meeting-stage.is-open").count() == 1, "по новой ссылке гость входит")
        # удалить идущую нельзя
        try:
            pp.purge_meeting(db, settings, m2)
            check(False, "удалить идущую встречу нельзя")
        except Exception as exc:  # noqa: BLE001
            check(getattr(exc, "status_code", 0) == 409, "удалить идущую встречу нельзя (409)")
        # завершение: комната закрыта, дорожки закрыты сразу
        ended_at = time.time()
        ms.end_meeting(db, m2)
        check(bool(wait_until(lambda: m2.room_name not in rooms(), 15)), "после завершения комната в LiveKit закрыта")
        man = room_dir(m2.room_name) / "manifest.json"
        closed = wait_until(lambda: man.is_file() and all("ended_epoch" in t for t in __import__("json").loads(man.read_text(encoding="utf-8")).get("tracks", [])), 20)
        check(bool(closed), "рекордер закрыл дорожки сразу после завершения (есть ended_epoch у всех)")
        if closed:
            import json as _json
            lag = max(t["ended_epoch"] for t in _json.loads(man.read_text(encoding="utf-8"))["tracks"]) - ended_at
            check(lag < 20, f"дорожки закрыты через {lag:.1f} с после завершения")
        status_text = wait_until(lambda: "заверш" in gc.inner_text("body").lower(), 20)
        check(bool(status_text), "у гостя видно, что встреча завершена")
        time.sleep(5)
        check(m2.room_name not in rooms(), "завершённая комната не воскресла (клиенты не переподключились)")
        code, payload = public(base, t2b)
        check(code == 200 and payload.get("ended"), "ссылка после завершения: встреча завершена")
        code, _ = public(base, t2b, "/join", {"display_name": "Опоздавший", "guest_id": uuid.uuid4().hex})
        check(code == 409, f"после завершения войти нельзя ({code})")
        b.close()
    # 4. удаление записанной встречи: каталог записи не возвращается
    room2 = m2.room_name
    pp.purge_meeting(db, settings, m2)
    db.expire_all()
    check(db.query(MeetingParticipant).filter(MeetingParticipant.meeting_id == m2.id).count() == 0, "удаление встречи: участников в базе нет")
    # каталог записи: рекордер держит файлы несколько секунд после закрытия комнаты — удаление повторяется (15/45/120 с)
    check(bool(wait_until(lambda: not room_dir(room2).exists(), 60, 2)), "удаление встречи: каталог записи удалён (с повторами)")
    to_check_later.append(room2)

    # 5. удаление проекта вместе с идущей встречей
    proj = Project(name=f"QA жизненный цикл {uuid.uuid4().hex[:6]}", description="временный, удаляется проверкой", created_by=admin.id, is_active=True)
    from app.app.services import agent_settings
    from app.app.services.storage import create_project_tree

    agent_settings.init_new_project(proj)
    db.add(proj)
    db.commit()
    db.refresh(proj)
    create_project_tree(settings, proj.id)
    m3, t3 = ms.create_meeting(db, settings, project=proj, actor=admin)
    ms.start_meeting(db, m3)
    print(f"временный проект #{proj.id}, встреча #{m3.id}", flush=True)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=ARGS)
        g = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"]).new_page()
        join(g, base, t3, "Гость В")
        tr = room_dir(m3.room_name) / "tracks"
        check(bool(wait_until(lambda: tr.is_dir() and any(tr.glob("guest-*.wav")), 25)), "во временном проекте идёт запись")
        pid, room3 = proj.id, m3.room_name
        pp.purge_project(db, settings, proj)
        db.expire_all()
        check(db.get(Project, pid) is None and db.query(Meeting).filter(Meeting.project_id == pid).count() == 0, "удаление проекта: проект и его встречи удалены из базы")
        check(not (settings.data_dir / "projects" / str(pid)).exists(), "удаление проекта: каталог проекта удалён")
        check(bool(wait_until(lambda: room3 not in rooms(), 20)), "удаление проекта с идущей встречей закрывает её комнату в LiveKit")
        code, _ = public(base, t3)
        check(code == 404, "ссылка встречи удалённого проекта не работает")
        b.close()
    to_check_later.append(room3)

    print(f"жду {args.linger} с: запись удалённых встреч не должна вернуться на диск…", flush=True)
    time.sleep(args.linger)
    for room in to_check_later:
        check(not room_dir(room).exists(), f"через {args.linger} с после удаления каталог записи {room} не вернулся")
    db.close()
    print("\nИТОГ:", "ВСЁ ПРОШЛО" if not FAILS else f"ПРОВАЛЕНО {len(FAILS)}: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
