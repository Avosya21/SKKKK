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
from app.app.services.project_purge import purge_project
from app.app.services.storage import create_project_tree, sanitize_display_name

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
def list_projects(
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
):
    rows = db.scalars(select(Project).where(Project.is_active.is_(True)).order_by(Project.id.desc())).all()
    return rows


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
    return project


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
):
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    purge_project(db, settings, project)
