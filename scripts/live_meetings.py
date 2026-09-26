"""Печатает идущие встречи и завершается кодом 0, если их нет, и 3, если есть.
Идущая = статус live И есть участник, который не вышел и был на связи в последние 10 минут (забытые «live» без людей не считаются).
Нужен restart.ps1: перезапуск сервера обрывает процессы записи и портит голосовые дорожки идущей встречи."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.app.db import SessionLocal  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from sqlalchemy import func  # noqa: E402

from app.app.models.meeting import STATUS_LIVE, Meeting, MeetingParticipant  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        rows = [
            m for m in db.query(Meeting).filter(Meeting.status == STATUS_LIVE).all()
            if db.query(MeetingParticipant.id).filter(
                MeetingParticipant.meeting_id == m.id, MeetingParticipant.left_at.is_(None),
                func.coalesce(MeetingParticipant.last_seen_at, MeetingParticipant.joined_at) >= cutoff,
            ).first()
        ]
        for m in rows:
            print(f"встреча #{m.id} проект {m.project_id}, идёт с {m.started_at}")
        return 3 if rows else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
