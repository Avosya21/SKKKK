"""Проверка бэкапа БД (пункт плана 5.6): резервная копия целая и её можно восстановить.

    venv\Scripts\python scripts\verify_backup.py [--backup data\backups\app-....db]

Шаги: integrity_check копии; те же таблицы, что в боевой БД; число строк ключевых таблиц не больше, чем в боевой
(копия — прошлое состояние); «восстановление» во временную папку и открытие; возраст копии. Только чтение боевой БД.
Код выхода 1 при любой проблеме.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY_TABLES = ["users", "projects", "artifacts", "chunks", "entities", "links", "meetings", "chat_messages"]
MAX_AGE_HOURS = 26


def tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def check_credentials_backup() -> list[str]:
    """База личных паролей: есть свежая копия, она целая и в ней есть таблица."""
    live = ROOT / "data" / "credentials.db"
    if not live.is_file():
        return []
    copies = sorted((ROOT / "data" / "backups").glob("credentials-*.db"), key=lambda p: p.stat().st_mtime)
    if not copies:
        return ["нет копии базы личных паролей (credentials-*.db)"]
    latest = copies[-1]
    age_h = (time.time() - latest.stat().st_mtime) / 3600
    print(f"Копия личных паролей: {latest.name}, возраст {age_h:.1f} ч")
    problems = []
    if age_h > MAX_AGE_HOURS:
        problems.append(f"копия личных паролей старше {MAX_AGE_HOURS} ч")
    con = sqlite3.connect(f"file:{latest.as_posix()}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            problems.append(f"личные пароли, integrity_check: {integrity}")
        if "personal_passwords" not in tables(con):
            problems.append("в копии личных паролей нет таблицы personal_passwords")
    finally:
        con.close()
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", type=Path)
    ap.add_argument("--live", type=Path, default=ROOT / "data" / "app.db")
    args = ap.parse_args()
    backups = sorted((ROOT / "data" / "backups").glob("app-*.db"), key=lambda p: p.stat().st_mtime)
    backup = args.backup or (backups[-1] if backups else None)
    problems: list[str] = []
    if backup is None or not backup.is_file():
        print("Бэкапов нет: запустите scripts\backup_db.ps1")
        return 1
    age_h = (time.time() - backup.stat().st_mtime) / 3600
    print(f"Копия: {backup.name}, {backup.stat().st_size / 1e6:.1f} МБ, возраст {age_h:.1f} ч")
    if age_h > MAX_AGE_HOURS:
        problems.append(f"копия старше {MAX_AGE_HOURS} ч (регулярный бэкап не идёт)")
    with tempfile.TemporaryDirectory() as tmp:
        restored = Path(tmp) / "restored.db"
        shutil.copy2(backup, restored)  # «восстановление» на копии, боевая БД не трогается
        con = sqlite3.connect(restored)
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        print("integrity_check:", integrity)
        if integrity != "ok":
            problems.append(f"integrity_check: {integrity}")
        live = sqlite3.connect(f"file:{args.live.as_posix()}?mode=ro", uri=True)
        missing = tables(live) - tables(con)
        if missing:
            problems.append(f"в копии нет таблиц: {sorted(missing)[:6]}")
        for table in KEY_TABLES:
            try:
                b, l = (con.execute(f"SELECT count(*) FROM {table}").fetchone()[0], live.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            except sqlite3.Error as exc:
                problems.append(f"{table}: {exc}")
                continue
            print(f"  {table}: копия {b}, боевая {l}")
            if b > l:
                problems.append(f"{table}: в копии больше строк, чем в боевой ({b} > {l})")
        live.close()
        con.close()
    problems += check_credentials_backup()
    print("ПРОБЛЕМЫ:" if problems else "Копия пригодна для восстановления.")
    for p in problems:
        print(" -", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
