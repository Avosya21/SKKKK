"""Отчёт: чьи голоса уже есть в общей базе и как они соотносятся с реестром людей проекта (PLATFORM-FOUNDATION-PLAN, ПФ5).

    venv\\Scripts\\python scripts\\voice_people_report.py --project 18

Ничего не меняет. Для каждого человека проекта показывает общие слепки с тем же именем и что с ними делать:
- сотрудник с согласием в карточке: привязать слепок к учётке (`scripts/link_voice_profiles.py link`, вручную);
- сотрудник без согласия: слепок к учётке привязывать нельзя;
- внешний человек (без учётки): по решению Д5 голос заказчика хранится только в проекте и только с отметкой менеджера
  о согласии; общий слепок надо решить (оставить, если согласие есть и голос нужен, или удалить).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int, required=True)
    args = ap.parse_args(argv)

    from sqlalchemy import select

    from app.app.db import SessionLocal
    import app.app.models  # noqa: F401
    from app.app.models.speaker_profile import SpeakerProfile
    from app.app.services import people, voice_link

    db = SessionLocal()
    try:
        pool = db.scalars(select(SpeakerProfile).where(SpeakerProfile.scope == "global")).all()
        print(f"Общих слепков голоса: {len(pool)}; людей в проекте {args.project}: {len(people.list_people(db, args.project))}")
        for person in people.list_people(db, args.project):
            aliases = {people.alias_key(a.alias) for a in people.aliases_of(db, person.id)}
            mine = [p for p in pool if people.alias_key(p.display_name) in aliases]
            if person.user_id:
                consent = voice_link.has_voice_consent(db, person.user_id)
                linked = [p for p in mine if p.user_id == person.user_id]
                todo = [p for p in mine if p.user_id is None]
                state = "согласие в карточке есть" if consent else "согласия в карточке нет"
                print(f"- {person.display_name} (сотрудник, {state}): слепков {len(mine)}, привязано к учётке {len(linked)}, не привязано {len(todo)}"
                      + (" -> можно привязать (link_voice_profiles.py link)" if todo and consent else ""))
            else:
                consent = "согласие отмечено" if person.voice_consent_at else "согласия нет"
                print(f"- {person.display_name} (внешний, {consent}): общих слепков {len(mine)}"
                      + (" -> нужно решение: по Д5 такой голос должен быть только в проекте" if mine else ""))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
