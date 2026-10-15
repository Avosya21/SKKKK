"""Компании из файла `Companies.txt` (корень проекта, как `Login_up.txt`): разбор и перенос в таблицу `companies`.

Строка файла: `Название | Исполнитель` (или `Заказчик`). Сторона пользователю не показывается — нужна анализу.
Файл читается при старте и при каждом открытии анкеты; убранная из файла компания скрывается, а не удаляется.
"""

from __future__ import annotations

import logging
import re
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


# ---------------------------------------------------------------- правка списка из портала (PLATFORM-FOUNDATION-PLAN, ПФ4)

SIDE_TO_LABEL = {SIDE_EXECUTOR: "Исполнитель", SIDE_CUSTOMER: "Заказчик"}


class CompanyError(ValueError):
    """Ошибка правки списка компаний; текст можно показывать пользователю."""


def write_companies_file(lines: list[CompanyLine], path: Path | None = None) -> None:
    """Записать список в файл: начальные комментарии сохраняются, строки — «Название | Сторона»."""
    path = path or get_settings().companies_path
    head: list[str] = []
    if path.is_file():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            if raw.strip() == "" or raw.lstrip().startswith("#"):
                head.append(raw)
            else:
                break
    body = [f"{line.name} | {SIDE_TO_LABEL[line.side]}" if line.side in SIDE_TO_LABEL else line.name for line in lines]
    path.write_text("\n".join(head + body) + "\n", encoding="utf-8")


_LEGAL_FORMS = re.compile(r"\b(ооо|оао|зао|пао|ао|ип|llc|ltd|inc)\b")


def loose_key(name: str | None) -> str:
    """Ключ одной компании под разными написаниями: без регистра, кавычек, пробелов и формы собственности
    («ООО «Пример-Альфа»» = «пример альфа» = «ПРИМЕР АЛЬФА ООО»)."""
    text = _LEGAL_FORMS.sub(" ", (name or "").casefold().replace("ё", "е"))
    return re.sub(r"[^0-9a-zа-я]", "", text)


def canonical_company(db: Session, name: str | None) -> Company | None:
    """Компания списка по названию (активная или скрытая): сначала точное написание без учёта регистра и лишних
    пробелов, затем то же название без кавычек и формы собственности — если такая компания в списке одна."""
    key = name_key(name or "")
    if not key:
        return None
    row = db.scalar(select(Company).where(Company.name_key == key))
    if row is not None:
        return row
    loose = loose_key(name)
    if not loose:
        return None
    hits = [c for c in db.scalars(select(Company)).all() if loose_key(c.name) == loose]
    active = [c for c in hits if c.active]
    hits = active or hits
    return hits[0] if len(hits) == 1 else None


def ensure_company(db: Session, name: str, side: str) -> Company:
    """Компания списка для стороны проекта (решение Р2, PEOPLE-FLOW-PLAN ЛГ1): найденная по названию, а новая —
    сразу в общий список со стороной по роли в проекте. Скрытая админом компания, снова выбранная в проекте, возвращается
    в список со своей прежней стороной. Сначала таблица сверяется с файлом: компания, уже записанная в файл, не должна
    добавиться заново со стороной этого проекта."""
    sync_companies(db)
    row = canonical_company(db, name)
    if row is None:
        return save_company(db, name, side)
    if not row.active:
        return save_company(db, row.name, row.side or side)
    return row


def save_company(db: Session, name: str, side: str, *, active: bool = True, path: Path | None = None) -> Company:
    """Добавить компанию или изменить её сторону / скрыть: сначала файл (источник истины), затем таблица.
    Убранная из файла компания скрывается, а не удаляется (у людей она остаётся). Переименование не поддерживается:
    новое название — новая компания, старую можно скрыть."""
    clean = " ".join((name or "").split())
    if not clean:
        raise CompanyError("Название не может быть пустым")
    if side not in (SIDE_EXECUTOR, SIDE_CUSTOMER):
        raise CompanyError("Сторона: исполнитель или заказчик")
    if any(ch in clean for ch in "|\r\n"):
        raise CompanyError("В названии нельзя использовать знак «|» и переносы строк")
    key = name_key(clean)
    out: list[CompanyLine] = []
    replaced = False
    for line in load_companies_file(path):
        if name_key(line.name) == key:
            replaced = True
            if active:
                out.append(CompanyLine(line.name, side))  # написание из файла сохраняется
        else:
            out.append(line)
    if not replaced and active:
        out.append(CompanyLine(clean, side))
    write_companies_file(out, path)
    sync_companies(db, path)
    row = canonical_company(db, clean)
    if row is None:
        raise CompanyError("Компания не найдена")
    if not active:
        row.active = False
    row.side = side if active else row.side
    db.commit()
    return row
