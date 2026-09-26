"""Доступ к проекту: свои + выданные участники; роль на проекте может отличаться от пароля."""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact
from app.app.models.chat_message import ChatMessage
from app.app.models.project import Project
from app.app.models.project_member import ProjectMember
from app.app.models.user import User


def user_project_ids(db: Session, user: User) -> set[int]:
    """Проекты логина: создал, загрузил, чат или выданный доступ."""
    created = select(Project.id).where(Project.created_by == user.id)
    uploaded = select(Artifact.project_id).where(Artifact.user_id == user.id)
    chatted = select(ChatMessage.project_id).where(ChatMessage.user_id == user.id)
    invited = select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
    return set(db.scalars(created.union(uploaded, chatted, invited)).all())


def can_access_project(user: User, project: Project, db: Session | None = None) -> bool:
    if user.role == "admin":
        return True
    if project.created_by == user.id:
        return True
    if db is None:
        return False
    return project.id in user_project_ids(db, user)


def is_project_owner(user: User, project: Project) -> bool:
    return user.role == "admin" or project.created_by == user.id


def membership_role(db: Session, user_id: int, project_id: int) -> str | None:
    row = db.scalar(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    return row


def project_role(db: Session, user: User, project: Project) -> str | None:
    """Роль участия в этом проекте: portal admin → admin; создатель → manager; иначе выдача или пароль."""
    if user.role == "admin":
        return "admin"
    if project.created_by == user.id:
        return "manager"
    invited = membership_role(db, user.id, project.id)
    if invited:
        return invited
    if can_access_project(user, project, db):
        return user.role if user.role in ("manager", "user") else "user"
    return None


def staff_ui_role(db: Session, user: User, project: Project) -> str:
    """Кнопки match / MCP / выдача: пароль `user` — всегда user, даже у создателя."""
    if user.role == "user":
        return "user"
    return project_role(db, user, project) or user.role


def can_grant_access(db: Session, user: User, project: Project) -> bool:
    return staff_ui_role(db, user, project) in ("admin", "manager")


def require_project(db: Session, user: User, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if not project or not project.is_active or not can_access_project(user, project, db):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def require_artifact_project(db: Session, user: User, artifact_id: str) -> Artifact:
    artifact = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
    if not artifact:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    require_project(db, user, artifact.project_id)
    return artifact


def require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def require_staff(user: User) -> None:
    if user.role not in ("admin", "manager"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def require_project_staff(db: Session, user: User, project_id: int) -> Project:
    project = require_project(db, user, project_id)
    if staff_ui_role(db, user, project) not in ("admin", "manager"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return project
