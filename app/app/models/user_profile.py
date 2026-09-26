"""Модель UserProfile — карточка человека (1:1 с users): ФИО, компания, должность, фото, согласие на голос."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class UserProfile(Base):
    """`users` — как человек входит; карточка — кто он. Новые сведения о человеке — новые таблицы
    со ссылкой `user_id` или ключи в `extra_json`, саму карточку не расширяем без нужды."""

    __tablename__ = "user_profiles"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    first_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    company_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("companies.id"), nullable=True, index=True)
    job_title: Mapped[str] = mapped_column(String(150), nullable=False, default="")
    # Хеш файла фото (он же версия для кеша браузера); пусто — фото нет.
    avatar_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Уведомление об автоматическом согласии: когда сохранил карточку и какую версию текста видел.
    # Пусто — согласие отозвано админом (слепок голоса с user_id не строится).
    voice_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voice_consent_text_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Анкета заполнена при входе; пусто — сессия не выдаётся.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
