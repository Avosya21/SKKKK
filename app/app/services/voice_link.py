"""Привязка слепков голоса к карточке сотрудника (задел: автосбор с дорожки созвона — отдельный план).

Правило: слепок получает `user_id` только при согласии человека на голос (уведомление в карточке);
без согласия привязка запрещена, отзыв согласия удаляет привязанные слепки (`user_profile.withdraw_voice_consent`).
Предложения привязки — только по точному совпадению имени («Имя Фамилия» карточки = имя слепка);
сами привязки делает человек (`scripts/link_voice_profiles.py`), автоматически ничего не привязывается.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.speaker_profile import SpeakerProfile
from app.app.models.user_profile import UserProfile
from app.app.services import user_profile as profiles


class VoiceLinkError(ValueError):
    pass


@dataclass(frozen=True)
class LinkSuggestion:
    profile_id: int
    scope: str
    voice_name: str
    user_id: int
    full_name: str


def _name_key(name: str) -> str:
    return " ".join((name or "").split()).casefold().replace("ё", "е")


def has_voice_consent(db: Session, user_id: int) -> bool:
    profile = profiles.get_profile(db, user_id)
    return bool(profile and profile.voice_consent_at)


def link_profile(db: Session, profile_id: int, user_id: int) -> SpeakerProfile:
    if not has_voice_consent(db, user_id):
        raise VoiceLinkError("Нет согласия на голос — привязывать слепок нельзя")
    row = db.get(SpeakerProfile, profile_id)
    if row is None:
        raise VoiceLinkError("Слепок не найден")
    row.user_id = user_id
    db.commit()
    return row


def unlink_profile(db: Session, profile_id: int) -> None:
    row = db.get(SpeakerProfile, profile_id)
    if row is not None:
        row.user_id = None
        db.commit()


def suggestions(db: Session) -> list[LinkSuggestion]:
    """Непривязанные слепки, чьё имя совпадает с «Имя Фамилия» (или «Фамилия Имя») сотрудника с согласием."""
    people: dict[str, list[UserProfile]] = {}
    for p in db.scalars(select(UserProfile).where(UserProfile.voice_consent_at.is_not(None))).all():
        for variant in (f"{p.first_name} {p.last_name}", f"{p.last_name} {p.first_name}"):
            people.setdefault(_name_key(variant), []).append(p)
    out = []
    for row in db.scalars(select(SpeakerProfile).where(SpeakerProfile.user_id.is_(None))).all():
        owners = {p.user_id: p for p in people.get(_name_key(row.display_name), [])}
        if len(owners) == 1:  # тёзки — не предлагаем: пусть решает человек
            owner = next(iter(owners.values()))
            out.append(LinkSuggestion(row.id, row.scope, row.display_name, owner.user_id, profiles.full_name(owner)))
    return out
