"""Личная доска проектов на стартовой странице: порядок карточек справа у каждого пользователя."""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.app.models.project import Project
from app.app.models.user import User
from app.app.models.user_dashboard import UserDashboardProject
from app.app.services.project_access import user_project_ids


def visible_project_ids(db: Session, user: User) -> set[int]:
    """Активные проекты, которые этот человек видит в списке."""
    q = select(Project.id).where(Project.is_active.is_(True))
    if user.role != "admin":
        allowed = user_project_ids(db, user)
        if not allowed:
            return set()
        q = q.where(Project.id.in_(allowed))
    return set(db.scalars(q).all())


def read_dashboard(db: Session, user: User) -> list[int]:
    """Порядок доски. Проект без доступа или неактивный в ответ не входит, строка при этом остаётся до следующей записи."""
    visible = visible_project_ids(db, user)
    rows = db.scalars(
        select(UserDashboardProject.project_id)
        .where(UserDashboardProject.user_id == user.id)
        .order_by(UserDashboardProject.sort_order.asc(), UserDashboardProject.id.asc())
    ).all()
    return [pid for pid in rows if pid in visible]


def replace_dashboard(db: Session, user: User, project_ids: list[int]) -> list[int]:
    """Заменить доску целиком. Чужой или повторяющийся id отклоняет всю запись."""
    if len(project_ids) != len(set(project_ids)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Duplicate project")
    visible = visible_project_ids(db, user)
    if any(pid not in visible for pid in project_ids):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Project not available")
    db.execute(delete(UserDashboardProject).where(UserDashboardProject.user_id == user.id))
    for order, pid in enumerate(project_ids):
        db.add(UserDashboardProject(user_id=user.id, project_id=pid, sort_order=order))
    db.commit()
    return list(project_ids)
