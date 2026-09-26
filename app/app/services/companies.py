"""Компании из файла `Companies.txt` (корень проекта, как `Login_up.txt`): разбор и перенос в таблицу `companies`.

Строка файла: `Название | Исполнитель` (или `Заказчик`). Сторона пользователю не показывается — нужна анализу.
Файл читается при старте и при каждом открытии анкеты; убранная из файла компания скрывается, а не удаляется.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import get_settings
from app.app.logging_setup import log_event
from app.app.models.company import SIDE_CUSTOMER, SIDE_EXECUTOR, Company

logger = logging.getLogger("skkkk.companies")

SIDE_LABELS = {"исполнитель": SIDE_EXECUTOR, "заказчик": SIDE_CUSTOMER}


@dataclass(frozen=True)
class CompanyLine:
    name: str
    side: str


def name_key(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def parse_companies(text: str) -> list[CompanyLine]:
    """Разобрать файл; неизвестная пометка стороны — предупреждение, сторона пустая."""
    out: dict[str, CompanyLine] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, mark = line.partition("|")
        name = " ".join(name.split())
        if not name:
            continue
        mark = mark.strip()
        side = SIDE_LABELS.get(mark.casefold(), "")
        if mark and not side:
            log_event(logger, "companies.unknown_side", level=logging.WARNING, line=lineno, name=name, mark=mark)
        key = name_key(name)
        if key not in out:
            out[key] = CompanyLine(name, side)
    return list(out.values())


def load_companies_file(path: Path | None = None) -> list[CompanyLine]:
    path = path or get_settings().companies_path
    if not path.is_file():
        return []
    return parse_companies(path.read_text(encoding="utf-8-sig"))


def sync_companies(db: Session, path: Path | None = None) -> list[Company]:
    """Файл → таблица. Возвращает активные компании по порядку файла."""
    lines = load_companies_file(path)
    existing = {c.name_key: c for c in db.scalars(select(Company)).all()}
    wanted: set[str] = set()
    changed = False
    for line in lines:
        key = name_key(line.name)
        wanted.add(key)
        row = existing.get(key)
        if row is None:
            row = Company(name=line.name, name_key=key, side=line.side, active=True)
            db.add(row)
            existing[key] = row
            changed = True
        elif (row.name, row.side, row.active) != (line.name, line.side, True):
            row.name, row.side, row.active = line.name, line.side, True
            changed = True
    for key, row in existing.items():
        if key not in wanted and row.active:
            row.active = False
            changed = True
    if changed:
        db.commit()
        log_event(logger, "companies.synced", active=len(wanted), total=len(existing))
    order = {name_key(line.name): i for i, line in enumerate(lines)}
    return sorted((existing[k] for k in wanted), key=lambda c: order[c.name_key])


def active_companies(db: Session) -> list[Company]:
    return sync_companies(db)
