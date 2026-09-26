"""Стресс-проверка записи звонка на РЕАЛЬНОМ сервере (рекордеры, LiveKit): песочный проект SMOKE_PROJECT_ID (29), два браузерных гостя.

Сценарии: запись обоих микрофонов растёт; убийство рекордера голоса посреди звонка -> сторож поднимает ровно один, дорожки
продолжаются в тех же файлах; после звонка процессов рекордеров не больше, чем было; лишних встреч/комнат не появилось.
НЕ запускать во время настоящего созвона (проверяется автоматически). Запуск: venv\Scripts\python.exe scripts\qa_call_stress.py"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import psutil  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import qa_room as qr  # noqa: E402

check, wait_until = qr.check, qr.wait_until


def recorder_procs(kind: str) -> list[int]:
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        argv = p.info["cmdline"] or []
        # сверяем именно аргумент-скрипт, а не подстроку (иначе задеваем чужие оболочки, в чьей командной строке лишь упомянут файл)
        if argv and "python" in Path(argv[0]).name.lower() and any(Path(a).name == kind for a in argv[1:]):
            out.append(p.info["pid"])
    return out


def wav_frames(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes()
    except Exception:  # noqa: BLE001
        return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:777")
    ap.add_argument("--secs", type=int, default=20)
    ap.add_argument("--restart", action="store_true", help="ещё и перезапустить API посреди звонка (restart.ps1 -Force -Llm gemma)")
    args = ap.parse_args()
    live = subprocess.run([sys.executable, str(ROOT / "scripts" / "live_meetings.py")], capture_output=True, text=True).stdout.strip()
    if live:
        print("Идёт настоящий созвон, стресс-тест не запускаю:\n" + live)
        return 2
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.meeting import Meeting

    settings = get_settings()
    s0 = SessionLocal()
    meetings_before = s0.query(Meeting).count()
    s0.close()
    voice0, screen0 = len(recorder_procs("voice_cleaner.py")), len(recorder_procs("screen_recorder.py"))
    print(f"рекордеры до: голос={voice0} экран={screen0}; встреч в БД={meetings_before}", flush=True)
    db, meeting, token = qr.make_meeting()
    room = meeting.room_name
    tracks_dir = settings.data_dir / "meetings" / room / "tracks"
    print(f"встреча #{meeting.id} {room}", flush=True)
    errors: list[str] = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
            pages = []
            for name in ("Гость А", "Гость Б"):
                ctx = b.new_context(ignore_https_errors=True, permissions=["microphone", "camera"])
                pg = ctx.new_page()
                pg.on("pageerror", lambda e: errors.append(str(e)))
                qr.join(pg, args.base, token, name)
                pages.append(pg)
            wavs = lambda: sorted(tracks_dir.glob("*.wav")) if tracks_dir.is_dir() else []  # noqa: E731
            wait_until(lambda: len(wavs()) >= 2, 40)
            check(len(wavs()) >= 2, f"записаны дорожки обоих гостей ({[w.name for w in wavs()]})")
            time.sleep(args.secs)
            f1 = {w.name: wav_frames(w) for w in wavs()}
            time.sleep(4)
            f2 = {w.name: wav_frames(w) for w in wavs()}
            check(all(f2[k] > f1[k] > 0 for k in f1), f"дорожки растут ({f1} -> {f2})")

            # убиваем рекордер голоса посреди звонка
            pids = recorder_procs("voice_cleaner.py")
            for pid in pids:
                try:
                    psutil.Process(pid).kill()
                except Exception:  # noqa: BLE001
                    pass
            print(f"убиты процессы голоса {pids}", flush=True)
            n_files = len(wavs())
            ok = wait_until(lambda: len(recorder_procs("voice_cleaner.py")) >= 1, 60)
            check(bool(ok), "сторож поднял рекордер голоса")
            t_kill = time.time()
            grown = wait_until(lambda: all(wav_frames(w) > f2[w.name] + 16000 for w in wavs() if w.name in f2), 120, 1.0)
            gap = time.time() - t_kill
            f4 = {w.name: wav_frames(w) for w in wavs()}
            print(f"разрыв записи после убийства рекордера: {gap:.0f} с", flush=True)
            check(bool(grown) and gap < 60, f"запись продолжилась в тех же файлах за {gap:.0f} с (<60): {f2} -> {f4}")
            check(len(wavs()) == n_files, f"после рестарта новых файлов нет ({len(wavs())} vs {n_files})")
            check(len(recorder_procs("voice_cleaner.py")) == voice0, f"процессов голоса ровно как было ({len(recorder_procs('voice_cleaner.py'))} vs {voice0})")

            if args.restart:
                before = sorted(recorder_procs("voice_cleaner.py") + recorder_procs("screen_recorder.py"))
                f5 = {w.name: wav_frames(w) for w in wavs()}
                t0 = time.time()
                # вывод в файл, не в pipe: сервер-потомок наследует pipe и run() не дождался бы EOF
                with open(ROOT / "data" / "logs" / "qa_restart.out", "w") as fh:
                    r = subprocess.run(["powershell", "-NoProfile", "-File", str(ROOT / "scripts" / "restart.ps1"), "-Force", "-Llm", "gemma"], stdout=fh, stderr=subprocess.STDOUT, timeout=400)
                print(f"restart.ps1 rc={r.returncode} за {time.time()-t0:.0f} с", flush=True)
                after = sorted(recorder_procs("voice_cleaner.py") + recorder_procs("screen_recorder.py"))
                check(before == after, f"рекордеры пережили перезапуск API теми же процессами ({before} -> {after})")
                ok = wait_until(lambda: all(wav_frames(w) > f5[w.name] + 16000 for w in wavs() if w.name in f5), 90, 1.0)
                check(bool(ok), "запись идёт после перезапуска API")
                check(len(wavs()) == n_files, "после перезапуска API новых файлов нет")
                check(wait_until(lambda: "Вы в комнате" in pages[0].inner_text("#meeting-room-status"), 60) is not None, "А остался в комнате после перезапуска API")
            # обрыв: закрыть вкладку Б без «выйти» (внезапно)
            pages[1].close()
            time.sleep(6)
            check(qr.tiles(pages[0]) >= 1, "у А комната жива после внезапного ухода Б")
            b.close()
    finally:
        qr.end_meeting(db, meeting)
    time.sleep(8)
    check(len(recorder_procs("voice_cleaner.py")) == voice0 and len(recorder_procs("screen_recorder.py")) == screen0, "после звонка число рекордеров прежнее")
    s1 = SessionLocal()
    check(s1.query(Meeting).count() == meetings_before + 1, f"создана ровно одна встреча (было {meetings_before}, стало {s1.query(Meeting).count()})")
    s1.close()
    final = {w.name: wav_frames(w) for w in sorted(tracks_dir.glob("*.wav"))}
    check(all(v > 0 for v in final.values()) and final, f"дорожки читаются после завершения: {final}")
    check(not errors, f"ошибок JS нет: {errors[:3]}")
    print("\nИТОГ:", "ВСЁ ПРОШЛО" if not qr.FAILS else f"ПРОВАЛЫ: {len(qr.FAILS)}")
    return 1 if qr.FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
