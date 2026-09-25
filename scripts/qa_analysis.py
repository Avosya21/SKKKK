"""Сквозная проверка «созвон → запись → анализ» без людей, на настоящей русской речи.

Создаёт встречу в песочнице, два участника публикуют заранее синтезированные реплики (scripts/qa_data_speech_*.wav) в живую комнату LiveKit,
рекордеры пишут дорожки; встреча завершается; запускается анализ (как кнопка «Проанализировать»); ждёт воркер и проверяет транскрипт.

Запуск:  venv\\Scripts\\python.exe scripts\\qa_analysis.py   (нужны запущенные сервер, воркер, LiveKit, рекордеры и LM Studio)
Код выхода 0 — всё прошло."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PY_VOICE = ROOT / "venvs" / "voice" / "Scripts" / "python.exe"
FAILS: list[str] = []


def check(cond: bool, message: str) -> None:
    print(("PASS " if cond else "FAIL ") + message, flush=True)
    if not cond:
        FAILS.append(message)


def main() -> int:
    only = int(sys.argv[sys.argv.index("--meeting") + 1]) if "--meeting" in sys.argv else 0  # повторить анализ уже записанной встречи
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.user import User
    from app.app.services import meeting_analyze as ma
    from app.app.services import meeting_recordings as rec
    from app.app.services import meetings as ms

    settings = get_settings()
    db = SessionLocal()
    project = db.get(Project, int(os.environ.get("SMOKE_PROJECT_ID", "29")))
    admin = db.query(User).filter(User.role == "admin").first()
    from app.app.models.meeting import Meeting

    if only:
        meeting = db.get(Meeting, only)
    else:
        meeting, _token = ms.create_meeting(db, settings, project=project, actor=admin)
        ms.start_meeting(db, meeting)
    print(f"встреча #{meeting.id}, комната {meeting.room_name}", flush=True)
    speakers = [] if only else [("Иван Петров", "qa-ivan-0001", "qa_data_speech_a.wav", 0), ("Елена Смирнова", "qa-elena-0002", "qa_data_speech_b.wav", 36)]
    procs = []
    for name, gid, wav, delay in speakers:
        payload = ms.join_guest(db, settings, meeting, name, gid)
        token = payload["livekit"]["token"]
        procs.append(subprocess.Popen([str(PY_VOICE), str(ROOT / "scripts" / "qa_publish_wav.py"), token, str(ROOT / "scripts" / wav), str(delay)],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8"))
    for p in procs:
        out, _ = p.communicate(timeout=180)
        print("  публикатор:", " ".join(x for x in out.split("\n") if x.startswith(("published", "done"))), flush=True)
    time.sleep(3)
    room_dir = rec.room_dir(settings.data_dir, meeting.room_name)
    tracks = ma.analyzable_tracks(settings.data_dir, meeting.room_name)
    print("  дорожки:", [(t["identity"][-6:], round(t["abs_path"].stat().st_size / 96000, 1)) for t in tracks], "(идентификатор, секунд)", flush=True)
    check(len(tracks) == 2, f"записаны обе дорожки ({len(tracks)})")
    for t in tracks:
        with wave.open(str(t["abs_path"]), "rb") as w:
            secs = w.getnframes() / w.getframerate()
        check(secs > 15, f"дорожка {t['identity'][-6:]} читается и содержит {secs:.0f} с звука")

    if not only:
        ms.end_meeting(db, meeting)
    time.sleep(2)
    state = ma.analyze_state(settings, meeting, db)
    check(state["can_analyze"] is True and state["track_count"] == 2, f"кнопка «Проанализировать» доступна (can_analyze={state['can_analyze']}, дорожек {state['track_count']})")
    ma.analyze_meeting(db, settings, meeting)
    check(True, "анализ поставлен в очередь")
    deadline = time.time() + 20 * 60
    last = ""
    while time.time() < deadline:
        db.expire_all()
        st = ma.analyze_state(settings, meeting, db)
        line = f"{st['analyze_status']} " + ",".join(f"{j.get('status')}" for j in st["analyze_jobs"])
        if line != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {line}", flush=True)
            last = line
        if st["analyze_status"] in ("done", "error"):
            break
        time.sleep(10)
    st = ma.analyze_state(settings, meeting, db)
    check(st["analyze_status"] == "done", f"анализ завершён без ошибок (статус {st['analyze_status']})")
    aids = [j.get("artifact_id") for j in st["analyze_jobs"] if j.get("artifact_id")]
    text = ""
    for aid in aids:
        art = db.scalar(__import__("sqlalchemy").select(Artifact).where(Artifact.artifact_id == aid))
        if art is not None:
            text += "\n" + (art.transcription_text or "")
    print("  --- транскрипт (начало) ---\n" + text.strip()[:900] + "\n  ---", flush=True)
    low = text.lower()
    hits = [w for w in ("покрыти", "профнастил", "пятниц", "печатн", "бюджет", "тестир") if w in low]
    check(len(hits) >= 4, f"в транскрипте узнаётся речь (найдено ключевых слов: {hits})")
    check(("иван" in low) or ("елена" in low) or ("гость" in low), "у реплик указан говорящий")
    print("\nИТОГ:", "ВСЁ ПРОШЛО" if not FAILS else f"ПРОВАЛЕНО {len(FAILS)}: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
