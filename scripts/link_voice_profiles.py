"""Привязка слепков голоса к карточкам сотрудников — только по подтверждению, ничего не привязывается само.

  python scripts/link_voice_profiles.py list            # предложения: имя слепка = «Имя Фамилия» сотрудника с согласием
  python scripts/link_voice_profiles.py link            # пройти по предложениям, на каждое — «да»/Enter
  python scripts/link_voice_profiles.py unlink <id слепка>

Без согласия человека на голос привязка запрещена (services/voice_link.py).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.app.db import SessionLocal  # noqa: E402
from app.app.services import voice_link  # noqa: E402

YES = {"да", "д", "yes", "y"}


def main(argv: list[str] | None = None, ask=input) -> int:
    ap = argparse.ArgumentParser(description="Слепки голоса ↔ карточки сотрудников")
    ap.add_argument("cmd", choices=["list", "link", "unlink"])
    ap.add_argument("profile_id", nargs="?", type=int)
    args = ap.parse_args(argv)

    db = SessionLocal()
    try:
        if args.cmd == "unlink":
            if not args.profile_id:
                ap.error("unlink: укажите id слепка")
            voice_link.unlink_profile(db, args.profile_id)
            print("Привязка снята.")
            return 0
        found = voice_link.suggestions(db)
        if not found:
            print("Предложений нет: совпадений имён слепков с карточками (с согласием на голос) не найдено.")
            return 0
        for s in found:
            print(f"  слепок #{s.profile_id} ({s.scope}) «{s.voice_name}» → {s.full_name} (user {s.user_id})")
        if args.cmd == "list":
            return 0
        linked = 0
        for s in found:
            if ask(f"Привязать слепок «{s.voice_name}» к {s.full_name}? [да / Enter — пропустить]: ").strip().casefold() in YES:
                voice_link.link_profile(db, s.profile_id, s.user_id)
                linked += 1
        print(f"Привязано: {linked}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
