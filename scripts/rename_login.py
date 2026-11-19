"""Одноразовая смена логина в app.db / credentials.db / файлах допуска.

Пример:
  venv\\Scripts\\python.exe scripts\\rename_login.py Anna anna@example.test
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _rename_in_text(path: Path, old: str, new: str) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8-sig")
    lines = []
    changed = False
    for line in text.splitlines():
        if line.strip().casefold() == old.casefold():
            lines.append(new)
            changed = True
        else:
            lines.append(line)
    if changed:
        path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    return changed


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: rename_login.py OLD NEW", file=sys.stderr)
        return 2
    old, new = sys.argv[1], sys.argv[2]
    old_key, new_key = old.casefold(), new.casefold()

    for name in ("Login_up.txt", "Panel_users.txt"):
        if _rename_in_text(ROOT / name, old, new):
            print(f"updated {name}")

    app_db = ROOT / "data" / "app.db"
    if app_db.is_file():
        con = sqlite3.connect(app_db)
        try:
            cur = con.execute(
                "UPDATE users SET name = ? WHERE lower(name) = ?",
                (new, old_key),
            )
            print(f"users updated: {cur.rowcount}")
            con.commit()
        finally:
            con.close()

    cred = ROOT / "data" / "credentials.db"
    if cred.is_file():
        con = sqlite3.connect(cred)
        try:
            cur = con.execute(
                "UPDATE personal_passwords SET login_key = ? WHERE login_key = ?",
                (new_key, old_key),
            )
            print(f"personal_passwords updated: {cur.rowcount}")
            con.commit()
        finally:
            con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
