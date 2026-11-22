"""Настройки интерфейса пользователя: стартовая страница, ширина колонки и высота задач."""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.app.models.user import User
from app.app.models.user_ui_setting import UserUiSetting

HOME_LIST = "list"
HOME_DASHBOARD = "dashboard"
HOME_LAYOUTS = frozenset({HOME_LIST, HOME_DASHBOARD})
# Левая колонка стартовой. Ниже нечитаемо, выше доска остаётся узкой полоской.
LIST_WIDTH_MIN = 272
LIST_WIDTH_MAX = 640
# Панель «Задачи» на доске. Ниже не видно строки, выше карточкам проекта не остаётся места.
TASKS_HEIGHT_MIN = 140
TASKS_HEIGHT_MAX = 640


def read_ui_settings(db: Session, user: User) -> tuple[str, int | None, int | None]:
    """Вид стартовой, ширина колонки и высота задач. Нет строки — список и размеры из вёрстки."""
    row = db.get(UserUiSetting, user.id)
    if row is None:
        return HOME_LIST, None, None
    home = row.home_layout if row.home_layout in HOME_LAYOUTS else HOME_LIST
    return home, row.list_width, row.tasks_height


def read_home_layout(db: Session, user: User) -> str:
    """Как открывается стартовая. Нет строки — список."""
    return read_ui_settings(db, user)[0]


def update_ui_settings(
    db: Session,
    user: User,
    fields: set[str],
    home: str | None,
    list_width: int | None,
    tasks_height: int | None,
) -> tuple[str, int | None, int | None]:
    """Записать только переданные поля. Чужое значение ширины, высоты или вида отклоняет запись."""
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty settings")
    row = db.get(UserUiSetting, user.id)
    if row is None:
        row = UserUiSetting(user_id=user.id, home_layout=HOME_LIST)
        db.add(row)
    if "home" in fields:
        if home not in HOME_LAYOUTS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown home layout")
        row.home_layout = home
    if "list_width" in fields:
        if list_width is not None and not LIST_WIDTH_MIN <= list_width <= LIST_WIDTH_MAX:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="List width out of range")
        row.list_width = list_width
    if "tasks_height" in fields:
        if tasks_height is not None and not TASKS_HEIGHT_MIN <= tasks_height <= TASKS_HEIGHT_MAX:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tasks height out of range")
        row.tasks_height = tasks_height
    db.commit()
    return row.home_layout, row.list_width, row.tasks_height
