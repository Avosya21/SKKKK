"""Проверка перед коммитом и отправкой: нет ли в файлах git имён людей и компаний из базы портала.

    venv\\Scripts\\python scripts\\scan_personal_data.py            # все отслеживаемые файлы
    venv\\Scripts\\python scripts\\scan_personal_data.py --staged   # только то, что в индексе (для хука)

Имена берутся из базы (люди проекта и их написания, карточки, логины, компании) и ищутся как целые слова без учёта регистра.
Находки показываются только здесь, в консоли; код выхода 1, если что-то найдено. В git не должно быть имён, компаний,
цитат и сумм проектов (DECISIONS: платформа памяти, приватность). Примеры в тестах и документах — вымышленные.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SKIP_PARTS = ("venv/", "node_modules/", "data/", "tools/lightrag/data/", ".git/", "static/vendor/", "logo_drafts/", "_ai_tools_stage/")
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".html", ".js", ".css", ".ps1", ".toml", ".cfg", ".ini", ".csv", ".sql"}
MIN_LEN = 4
# Слова, которые встречаются в коде и документах сами по себе и именем человека не являются
COMMON = {"test", "admin", "user", "manager", "maria", "anna", "ivan", "demo", "guest", "пример", "тест",
          # самые обычные фамилии из учебных примеров: именем конкретного человека не считаются
          "иванов", "иванова", "петров", "петрова", "сидоров", "сидорова", "смирнов", "смирнова", "кузнецов", "кузнецова"}


def _names(db) -> set[str]:
    """Что искать: полные имена (два слова и больше), фамилии из карточек, названия компаний и логины-почты. Одиночные имена
    («Анна», «Иван») и служебные слова не ищутся: они встречаются в коде и документах сами по себе."""
    from sqlalchemy import select

    from app.app.models.company import Company
    from app.app.models.person import Person, PersonAlias
    from app.app.models.user import User
    from app.app.models.user_profile import UserProfile

    full: set[str] = set()
    single: set[str] = set()
    for (name,) in db.execute(select(Person.display_name)).all():
        full.add(name)
    for (alias,) in db.execute(select(PersonAlias.alias)).all():
        full.add(alias)
    for first, last in db.execute(select(UserProfile.first_name, UserProfile.last_name)).all():
        full.update({f"{first} {last}", f"{last} {first}"})
        single.add(last)
    for (login,) in db.execute(select(User.name)).all():
        if "@" in (login or ""):
            single.add(login)
    for (name,) in db.execute(select(Company.name)).all():
        single.add(name)
    out: set[str] = set()
    for item in full:
        text = " ".join((item or "").split())
        text = text.partition("(")[0].strip()
        if len(text.split()) >= 2 and len(text) >= 7:
            out.add(text)
    for item in single:
        text = " ".join((item or "").split())
        if len(text) >= MIN_LEN and text.casefold() not in COMMON and not text.isdigit():
            out.add(text)
    return out


def _files(staged: bool) -> list[Path]:
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=AM"] if staged else ["git", "ls-files"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout.splitlines()
    return [ROOT / f for f in out if f and Path(f).suffix.lower() in TEXT_SUFFIXES and not any(part in f.replace("\\", "/") for part in SKIP_PARTS)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staged", action="store_true", help="только файлы в индексе git")
    args = ap.parse_args(argv)

    from app.app.db import SessionLocal
    import app.app.models  # noqa: F401

    db = SessionLocal()
    try:
        names = _names(db)
    finally:
        db.close()
    if not names:
        print("В базе нет имён для проверки.")
        return 0
    pattern = re.compile(r"(?<![\w])(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![\w])", re.IGNORECASE)
    found = 0
    for path in _files(args.staged):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            for match in pattern.finditer(line):
                found += 1
                if found <= 60:
                    print(f"{path.relative_to(ROOT).as_posix()}:{number}: «{match.group(1)}»")
    print(f"Проверено имён из базы: {len(names)}. Совпадений: {found}.")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
