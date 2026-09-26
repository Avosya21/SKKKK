"""Карточки пользователей из консоли: пункт 8 меню запуска `main` и `scripts/user_profile.py`.

Только здесь меняется компания человека и согласие на голос (отзыв удаляет слепки); сброс карточки
заставляет заполнить её заново при следующем входе. В портале этих действий нет — решение владельца.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import get_settings
from app.app.logging_setup import log_event
from app.app.models.company import Company
from app.app.models.user import User
from app.app.services import user_profile as profiles
from app.app.services.companies import active_companies, name_key
from app.app.services.login_allowlist import load_allowlist, login_key

logger = logging.getLogger("skkkk.profile")

YES = {"да", "д", "yes", "y"}


@dataclass(frozen=True)
class CardRow:
    login: str
    user_id: int | None  # None — ещё ни разу не входил: карточки нет
    full_name: str
    company: str
    job_title: str
    has_photo: bool
    voice: bool
    complete: bool


def card_rows(db: Session) -> list[CardRow]:
    allow = load_allowlist(get_settings().login_allowlist_path)
    users = {login_key(u.name): u for u in db.scalars(select(User).order_by(User.id)).all()}
    companies = {c.id: c.name for c in db.scalars(select(Company)).all()}
    rows = []
    for key, login in allow.items():
        user = users.get(key)
        profile = profiles.get_profile(db, user.id) if user else None
        rows.append(
            CardRow(
                login=login,
                user_id=user.id if user else None,
                full_name=profiles.full_name(profile),
                company=companies.get(profile.company_id, "") if profile and profile.company_id else "",
                job_title=profile.job_title if profile else "",
                has_photo=bool(profile and profile.avatar_key),
                voice=bool(profile and profile.voice_consent_at),
                complete=profiles.is_complete(profile),
            )
        )
    return rows


def print_rows(rows: list[CardRow], out: Callable[[str], None] = print) -> None:
    width = max([len(r.login) for r in rows] + [5])
    out(f"  {'№':>3}  {'Логин':<{width}}  {'Имя Фамилия':<24}  {'Компания':<14}  {'Должность':<22}  Фото  Голос  Карточка")
    for i, r in enumerate(rows, 1):
        state = "заполнена" if r.complete else ("не входил" if r.user_id is None else "нет")
        out(
            f"  {i:>3}  {r.login:<{width}}  {r.full_name[:24]:<24}  {r.company[:14]:<14}  {r.job_title[:22]:<22}  "
            f"{'да' if r.has_photo else '—':<4}  {'да' if r.voice else '—':<5}  {state}"
        )


def resolve(choice: str, rows: list[CardRow]) -> CardRow | None:
    choice = (choice or "").strip()
    if choice.isdigit():
        n = int(choice)
        return rows[n - 1] if 1 <= n <= len(rows) else None
    key = login_key(choice)
    return next((r for r in rows if login_key(r.login) == key), None)


def find_company(choice: str, companies: list[Company]) -> Company | None:
    choice = (choice or "").strip()
    if choice.isdigit():
        n = int(choice)
        return companies[n - 1] if 1 <= n <= len(companies) else None
    key = name_key(choice)
    return next((c for c in companies if c.name_key == key), None)


def set_company(db: Session, row: CardRow, company: Company, *, source: str) -> None:
    profiles.set_company(db, row.user_id, company.id)
    log_event(logger, "profile.company_set", login=row.login, company=company.name, source=source)


def withdraw_voice(db: Session, row: CardRow, *, source: str) -> int:
    removed = profiles.withdraw_voice_consent(db, row.user_id)
    log_event(logger, "profile.voice_withdrawn", login=row.login, removed_prints=removed, source=source)
    return removed


def restore_voice(db: Session, row: CardRow, *, source: str) -> None:
    profiles.restore_voice_consent(db, row.user_id)
    log_event(logger, "profile.voice_restored", login=row.login, source=source)


def reset_card(db: Session, row: CardRow, *, source: str) -> None:
    profiles.reset_profile(db, row.user_id)
    log_event(logger, "profile.reset", login=row.login, source=source)


def interactive(
    db: Session,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> bool:
    """Пункт 8: список → номер или логин → действие → «да». True — что-то изменено."""
    rows = card_rows(db)
    out("")
    if not rows:
        out("Список логинов пуст (Login_up.txt).")
        return False
    print_rows(rows, out)
    try:
        row = resolve(ask("Номер или логин (Enter — назад): "), rows)
    except EOFError:
        return False
    if row is None:
        out("Не выбран.")
        return False
    if row.user_id is None:
        out(f"{row.login} ещё ни разу не входил — карточки нет.")
        return False
    out(f"{row.login}: {row.full_name or 'без имени'}, {row.company or 'компания не указана'}")
    out("  1 — сменить компанию")
    out("  2 — " + ("отозвать согласие на голос (слепки удалятся)" if row.voice else "вернуть согласие на голос"))
    out("  3 — сбросить карточку (заполнит заново при следующем входе, сессии закроются)")
    try:
        action = ask("Действие [1/2/3, Enter — назад]: ").strip()
        if action == "1":
            companies = active_companies(db)
            for i, c in enumerate(companies, 1):
                out(f"  {i} — {c.name}")
            company = find_company(ask("Номер или название компании: "), companies)
            if company is None:
                out("Компания не найдена (список — Companies.txt).")
                return False
            question, run = f"Назначить {row.login} компанию {company.name}?", lambda: set_company(db, row, company, source="startup_menu")
        elif action == "2":
            if row.voice:
                question, run = f"Отозвать согласие {row.login} на голос и удалить слепки?", lambda: withdraw_voice(db, row, source="startup_menu")
            else:
                question, run = f"Вернуть согласие {row.login} на голос?", lambda: restore_voice(db, row, source="startup_menu")
        elif action == "3":
            question, run = f"Сбросить карточку {row.login}?", lambda: reset_card(db, row, source="startup_menu")
        else:
            return False
        if ask(f"{question} [да / Enter — отмена]: ").strip().casefold() not in YES:
            out("Отменено.")
            return False
    except EOFError:
        return False
    try:
        run()
    except profiles.ProfileError as exc:
        out(f"Не удалось: {exc}")
        return False
    out("Готово.")
    return True

