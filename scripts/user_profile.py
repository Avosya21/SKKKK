"""Карточки пользователей из командной строки — когда портал запущен службой, а не из PyCharm.

  python scripts/user_profile.py list
  python scripts/user_profile.py set-company <логин | номер> <компания | номер>
  python scripts/user_profile.py withdraw-voice <логин | номер>     # отозвать согласие, слепки удаляются
  python scripts/user_profile.py restore-voice <логин | номер>
  python scripts/user_profile.py reset <логин | номер>              # заполнит карточку заново при входе

Изменения спрашивают подтверждение; `--yes` — без вопроса. В PyCharm то же самое — пункт 8 меню запуска.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.app.db import SessionLocal  # noqa: E402
from app.app.services import user_profile_console as console  # noqa: E402
from app.app.services.companies import active_companies  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Карточки пользователей")
    ap.add_argument("cmd", choices=["list", "set-company", "withdraw-voice", "restore-voice", "reset"])
    ap.add_argument("login", nargs="?", help="логин или номер из list")
    ap.add_argument("company", nargs="?", help="для set-company: название или номер компании")
    ap.add_argument("--yes", action="store_true", help="без подтверждения")
    args = ap.parse_args(argv)

    db = SessionLocal()
    try:
        rows = console.card_rows(db)
        if args.cmd == "list":
            console.print_rows(rows)
            return 0
        row = console.resolve(args.login or "", rows)
        if row is None:
            print(f"Логин {args.login!r} не найден.", file=sys.stderr)
            return 1
        if row.user_id is None:
            print(f"{row.login} ещё ни разу не входил — карточки нет.", file=sys.stderr)
            return 1
        if args.cmd == "set-company":
            companies = active_companies(db)
            company = console.find_company(args.company or "", companies)
            if company is None:
                print("Компания не найдена. Есть: " + ", ".join(c.name for c in companies), file=sys.stderr)
                return 1
            question = f"Назначить {row.login} компанию {company.name}?"
            run = lambda: console.set_company(db, row, company, source="script")  # noqa: E731
        elif args.cmd == "withdraw-voice":
            question = f"Отозвать согласие {row.login} на голос и удалить слепки?"
            run = lambda: print(f"Удалено слепков: {console.withdraw_voice(db, row, source='script')}")  # noqa: E731
        elif args.cmd == "restore-voice":
            question = f"Вернуть согласие {row.login} на голос?"
            run = lambda: console.restore_voice(db, row, source="script")  # noqa: E731
        else:
            question = f"Сбросить карточку {row.login}?"
            run = lambda: console.reset_card(db, row, source="script")  # noqa: E731
        if not args.yes and input(f"{question} [да / Enter — отмена]: ").strip().casefold() not in console.YES:
            print("Отменено.")
            return 1
        run()
        print("Готово.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
