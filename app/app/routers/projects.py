from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import get_current_user, require_role
from app.app.models.project import Project
from app.app.models.user import User
from app.app.schemas.project import ProjectCreate, ProjectOut
from app.app.services.project_access import is_project_owner, require_project, user_project_ids
from app.app.services.project_purge import purge_project
from app.app.services.storage import create_project_tree, sanitize_display_name
from app.app.services.user_activity import ACTION_CREATE, ACTION_DELETE, BLOCK_PROJECT, log_activity

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
def list_projects(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    q = select(Project).where(Project.is_active.is_(True))
    if user.role != "admin":
        q = q.where(Project.id.in_(user_project_ids(db, user) or {-1}))
    return db.scalars(q.order_by(Project.id.desc())).all()


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    body: ProjectCreate,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    project = Project(
        name=sanitize_display_name(body.name),
        description=body.description,
        created_by=user.id,
        is_active=True,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    create_project_tree(settings, project.id)
    log_activity(
        db,
        user_id=user.id,
        project_id=project.id,
        block=BLOCK_PROJECT,
        action=ACTION_CREATE,
        title="Создал проект",
        content=project.name,
        target_kind="project",
        target_id=project.id,
    )
    return project


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    return require_project(db, user, project_id)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    project = require_project(db, user, project_id)
    if not is_project_owner(user, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    name = project.name
    purge_project(db, settings, project)
    log_activity(
        db,
        user_id=user.id,
        project_id=None,
        block=BLOCK_PROJECT,
        action=ACTION_DELETE,
        title="Удалил проект",
        content=f"{name} (id={project_id})",
        target_kind="project",
        target_id=project_id,
    )
