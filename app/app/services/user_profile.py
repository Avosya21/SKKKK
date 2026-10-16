"""Карточка пользователя: проверка полей, заполнение при первом входе, правка своей карточки, действия админа.

Компанию меняет только админ (`set_company`); ФИО, должность и фото — сам человек (`update_own`, `replace_avatar`).
Согласие на голос — уведомление на форме: сохранение карточки = согласие (дата и версия текста).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.company import Company
from app.app.models.user import User
from app.app.models.user_profile import UserProfile
from app.app.services import avatars

# Текст уведомления на форме карточки. Меняется текст — растёт версия: в карточке видно, что видел человек.
CONSENT_TEXT_VERSION = "2026-09-26"
CONSENT_TEXT = (
    "Сохраняя карточку, вы соглашаетесь, что ваш голос в записях созвонов используется "
    "для автоматического распознавания, кто говорит."
)
CONSENT_DETAILS = (
    "Храним числовой слепок голоса, а не саму запись. Он нужен, чтобы в расшифровке созвона "
    "ваши реплики были подписаны вашим именем. Отозвать согласие можно через администратора — "
    "слепки будут удалены."
)

NAME_MAX = 100
JOB_MIN, JOB_MAX = 2, 150
_NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё]+(?:[ '’\-][A-Za-zА-Яа-яЁё]+)*$")


class ProfileError(ValueError):
    """Поле карточки не подходит. `field` — какое поле подсветить в форме."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class ProfileFields:
    last_name: str
    first_name: str
    job_title: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_name(value: str, field: str, label: str) -> str:
    value = " ".join((value or "").split())
    if not value:
        raise ProfileError(field, f"Укажите {label}")
    if len(value) > NAME_MAX:
        raise ProfileError(field, f"{label.capitalize()}: не длиннее {NAME_MAX} символов")
    if not _NAME_RE.match(value):
        raise ProfileError(field, f"{label.capitalize()}: только буквы, пробел, дефис или апостроф")
    return value


def clean_fields(last_name: str, first_name: str, job_title: str) -> ProfileFields:
    job = " ".join((job_title or "").split())
    if len(job) < JOB_MIN:
        raise ProfileError("job_title", "Укажите должность")
    if len(job) > JOB_MAX:
        raise ProfileError("job_title", f"Должность: не длиннее {JOB_MAX} символов")
    return ProfileFields(
        last_name=_clean_name(last_name, "last_name", "фамилию"),
        first_name=_clean_name(first_name, "first_name", "имя"),
        job_title=job,
    )


def active_company(db: Session, company_id: int | None) -> Company:
    company = db.get(Company, company_id) if company_id else None
    if company is None or not company.active:
        raise ProfileError("company_id", "Выберите компанию из списка")
    return company


def get_profile(db: Session, user_id: int) -> UserProfile | None:
    return db.get(UserProfile, user_id)


def full_name(profile: UserProfile | None) -> str:
    if profile is None:
        return ""
    return " ".join(p for p in (profile.first_name, profile.last_name) if p)


def is_complete(profile: UserProfile | None) -> bool:
    return bool(
        profile
        and profile.completed_at
        and profile.last_name
        and profile.first_name
        and profile.job_title
        and profile.company_id
        and profile.avatar_key
    )


def extra(profile: UserProfile) -> dict:
    try:
        data = json.loads(profile.extra_json or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _get_or_new(db: Session, user_id: int) -> UserProfile:
    profile = db.get(UserProfile, user_id)
    if profile is None:
        profile = UserProfile(user_id=user_id, extra_json="{}")
        db.add(profile)
    return profile


def complete_onboarding(
    db: Session,
    user: User,
    *,
    last_name: str,
    first_name: str,
    job_title: str,
    company_id: int | None,
    avatar_bytes: bytes,
) -> UserProfile:
    """Анкета при первом входе: всё обязательно, сохраняется разом. Сохранение = согласие на голос."""
    fields = clean_fields(last_name, first_name, job_title)
    existing = db.get(UserProfile, user.id)
    if existing is not None and existing.company_id:
        # Компанию уже назначил админ (карточку сбрасывали) — человек её не меняет.
        company = db.get(Company, existing.company_id)
    else:
        company = active_company(db, company_id)
    if not avatar_bytes:
        raise ProfileError("avatar", "Добавьте фото")
    try:
        key = avatars.save_avatar(user.id, avatar_bytes, old_key=existing.avatar_key if existing else None)
    except avatars.AvatarError as exc:
        raise ProfileError("avatar", str(exc)) from None
    profile = _get_or_new(db, user.id)
    profile.last_name, profile.first_name, profile.job_title = fields.last_name, fields.first_name, fields.job_title
    profile.company_id = company.id
    profile.avatar_key = key
    now = _utcnow()
    profile.voice_consent_at = now
    profile.voice_consent_text_version = CONSENT_TEXT_VERSION
    profile.completed_at = now
    db.commit()
    db.refresh(profile)
    _sync_people_company(db, user.id, existing.company_id if existing is not None else None)
    return profile


def update_own(db: Session, user: User, *, last_name: str, first_name: str, job_title: str) -> UserProfile:
    """Правка своей карточки: ФИО и должность. Компания и согласие здесь не меняются."""
    profile = db.get(UserProfile, user.id)
    if profile is None:
        raise ProfileError("profile", "Карточка ещё не заполнена")
    fields = clean_fields(last_name, first_name, job_title)
    profile.last_name, profile.first_name, profile.job_title = fields.last_name, fields.first_name, fields.job_title
    db.commit()
    db.refresh(profile)
    return profile


def replace_avatar(db: Session, user: User, avatar_bytes: bytes) -> UserProfile:
    profile = db.get(UserProfile, user.id)
    if profile is None:
        raise ProfileError("profile", "Карточка ещё не заполнена")
    try:
        profile.avatar_key = avatars.save_avatar(user.id, avatar_bytes, old_key=profile.avatar_key)
    except avatars.AvatarError as exc:
        raise ProfileError("avatar", str(exc)) from None
    db.commit()
    db.refresh(profile)
    return profile


# --- действия админа (меню запуска, scripts/user_profile.py) ---


def set_company(db: Session, user_id: int, company_id: int) -> UserProfile:
    company = active_company(db, company_id)
    profile = _get_or_new(db, user_id)
    old = profile.company_id
    profile.company_id = company.id
    db.commit()
    db.refresh(profile)
    _sync_people_company(db, user_id, old)
    return profile


def has_card(profile: UserProfile | None) -> bool:
    """Карточка, без которой учётку нельзя добавить в проект (Р6, PEOPLE-FLOW-PLAN ЛГ3): фамилия, имя и компания."""
    return bool(profile and profile.last_name and profile.first_name and profile.company_id)


def fill_card_for_member(db: Session, user_id: int, *, last_name: str, first_name: str, company_id: int | None) -> UserProfile:
    """Менеджер выдаёт доступ сотруднику без карточки: фамилия, имя и компания. Фото, должность и согласие на голос
    человек заполнит сам при первом входе (анкета не меняет компанию, назначенную здесь). Уже заполненное не перетирается."""
    company = active_company(db, company_id)
    last = _clean_name(last_name, "last_name", "фамилию")
    first = _clean_name(first_name, "first_name", "имя")
    profile = _get_or_new(db, user_id)
    old = profile.company_id
    profile.last_name = profile.last_name or last
    profile.first_name = profile.first_name or first
    profile.company_id = profile.company_id or company.id
    db.commit()
    db.refresh(profile)
    _sync_people_company(db, user_id, old)
    return profile


def _sync_people_company(db: Session, user_id: int, old_company_id: int | None) -> None:
    """Компания в карточке сотрудника сменилась — у его людей в проектах тоже (если там была прежняя компания карточки
    или пусто); сторона — от компании в проекте (Р1)."""
    from app.app.services import people

    profile = db.get(UserProfile, user_id)
    if profile is None or not profile.company_id or profile.company_id == old_company_id:
        return
    people.sync_user_company(db, user_id, old_company_id)
    db.commit()


def withdraw_voice_consent(db: Session, user_id: int) -> int:
    """Отозвать согласие на голос: слепки этого человека удаляются. Возвращает число удалённых слепков."""
    from app.app.models.speaker_profile import SpeakerProfile

    profile = db.get(UserProfile, user_id)
    if profile is not None:
        profile.voice_consent_at = None
        profile.voice_consent_text_version = None
    rows = db.scalars(select(SpeakerProfile).where(SpeakerProfile.user_id == user_id)).all()
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)


def restore_voice_consent(db: Session, user_id: int) -> None:
    profile = db.get(UserProfile, user_id)
    if profile is None:
        raise ProfileError("profile", "Карточка ещё не заполнена")
    profile.voice_consent_at = _utcnow()
    profile.voice_consent_text_version = CONSENT_TEXT_VERSION
    db.commit()


def reset_profile(db: Session, user_id: int) -> None:
    """Сбросить карточку: человек заполнит её заново при следующем входе. Компания (её назначил админ) остаётся."""
    profile = db.get(UserProfile, user_id)
    if profile is None:
        return
    avatars.remove_avatar(user_id, profile.avatar_key)
    profile.avatar_key = None
    profile.completed_at = None
    db.commit()


def _rank(profile: UserProfile) -> tuple[bool, float]:
    """Какая карточка полнее: заполненная, затем заполненная позже (SQLite отдаёт время без пояса)."""
    when = profile.completed_at
    stamp = when.replace(tzinfo=None).timestamp() if when else 0.0
    return is_complete(profile), stamp


def merge_profiles(db: Session, *, winner_id: int, loser_id: int) -> None:
    """Склейка дублей пользователей: остаётся одна карточка, файлы фото не теряются.

    У победителя нет карточки — переходит карточка проигравшего (с фото); есть у обоих — остаётся
    более полная (заполнена позже), вторая удаляется вместе с фото.
    """
    loser = db.get(UserProfile, loser_id)
    if loser is None:
        return
    winner = db.get(UserProfile, winner_id)
    if winner is not None and _rank(winner) >= _rank(loser):
        avatars.remove_user_files(loser_id)
        db.delete(loser)
        db.flush()
        return
    if winner is not None:
        avatars.remove_avatar(winner_id, winner.avatar_key)
        db.delete(winner)
        db.flush()
    data = {c.name: getattr(loser, c.name) for c in UserProfile.__table__.columns if c.name != "user_id"}
    db.delete(loser)
    db.flush()
    db.add(UserProfile(user_id=winner_id, **data))
    db.flush()
    avatars.move_user_files(loser_id, winner_id)
