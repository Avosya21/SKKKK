"""Настройка интерфейса пользователя (таблица user_ui_settings): стартовая страница, ширина колонки и высота задач.

Нет строки — список. Дашборд пишется только явным выбором в настройках.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.app.db import Base


class UserUiSetting(Base):
    __tablename__ = "user_ui_settings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    home_layout: Mapped[str] = mapped_column(String(16), nullable=False, default="list")
    # Ширина левой колонки «Все проекты» в пикселях. Пусто — ширина из вёрстки.
    list_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Высота панели «Задачи» на доске в пикселях. Пусто — высота из вёрстки.
    tasks_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
