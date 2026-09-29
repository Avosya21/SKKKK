"""Печатает идущие встречи и завершается кодом 0, если их нет, и 3, если есть.
Идущая = статус live И есть участник, который не вышел и был на связи в последние 10 минут (забытые «live» без людей не считаются).
Правило общее с полоской «Идёт созвон» на странице проекта: services/meetings.ongoing_meetings.
Нужен restart.ps1: перезапуск сервера обрывает процессы записи и портит голосовые дорожки идущей встречи."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.app.db import SessionLocal  # noqa: E402
from app.app.services.meetings import ongoing_meetings  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        rows = ongoing_meetings(db)
        for m, _people in rows:
            print(f"встреча #{m.id} проект {m.project_id}, идёт с {m.started_at}")
        return 3 if rows else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
