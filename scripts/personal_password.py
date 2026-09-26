"""Личные пароли из командной строки — на случай, когда портал запущен службой, а не из PyCharm.

  python scripts/personal_password.py list
  python scripts/personal_password.py reset <логин | номер>          # спросит подтверждение
  python scripts/personal_password.py reset <логин | номер> --yes

Работает с базой `data/credentials.db` напрямую, вход на сайт не нужен. В PyCharm то же самое — пункт 7 меню запуска.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.app.services import personal_password_console as console  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Личные пароли: список и сброс")
    ap.add_argument("cmd", choices=["list", "reset"])
    ap.add_argument("login", nargs="?", help="логин или номер из list")
    ap.add_argument("--yes", action="store_true", help="сбросить без вопроса")
    args = ap.parse_args(argv)

    rows = console.login_rows()
    if args.cmd == "list":
        console.print_rows(rows)
        return 0
    if not args.login:
        ap.error("reset: укажите логин или номер")
    row = console.resolve(args.login, rows)
    if row is None:
        print(f"Логин {args.login!r} не найден.", file=sys.stderr)
        return 1
    if not args.yes:
        answer = input(f"Сбросить личный пароль {row.login}? [да / Enter — отмена]: ").strip().casefold()
        if answer not in {"да", "д", "yes", "y"}:
            print("Отменено.")
            return 1
    console.reset_login(row, source="script")
    print(f"Пароль {row.login} сброшен: сессии закрыты, при следующем входе он создаст новый.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
