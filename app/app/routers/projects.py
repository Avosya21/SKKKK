"""Проекты: список, создание, карточка, переименование, предпросмотр и полное удаление."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import get_current_user, require_role
from app.app.models.project import Project
from app.app.models.user import User
from app.app.schemas.project import ProjectCreate, ProjectOut, ProjectRename
from app.app.services.project_access import is_project_owner, require_project, user_project_ids
from app.app.services.project_purge import purge_project
from app.app.services.storage import create_project_tree, normalize_project_name
from app.app.services.user_activity import ACTION_CREATE, ACTION_DELETE, ACTION_RENAME, BLOCK_PROJECT, log_activity

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
    rows = db.scalars(q.order_by(Project.id.desc())).all()
    names = creator_names(db, {p.created_by for p in rows})
    return [ProjectOut.model_validate(p).model_copy(update={"creator_name": names.get(p.created_by)}) for p in rows]


def creator_names(db: Session, user_ids: set[int]) -> dict[int, str]:
    """Автор проекта для админа: «Фамилия Имя» из карточки, без карточки — логин."""
    from app.app.models.user_profile import UserProfile

    if not user_ids:
        return {}
    rows = db.execute(
        select(User.id, User.name, UserProfile.last_name, UserProfile.first_name)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .where(User.id.in_(user_ids))
    ).all()
    return {uid: " ".join(x for x in (last or "", first or "") if x.strip()).strip() or login for uid, login, last, first in rows}


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    body: ProjectCreate,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    name = normalize_project_name(body.name)
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Project name required")
    project = Project(
        name=name,
        description=body.description,
        created_by=user.id,
        is_active=True,
    )
    from app.app.services import agent_settings, people

    agent_settings.init_new_project(project)  # новый проект: без якорей 1С и без названий компаний в правилах сторон
    names = [people.alias_key(p.name) for p in body.customer_people]
    if any(not n for n in names) or len(set(names)) != len(names):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Имена людей заказчика должны быть непустыми и не повторяться")
    db.add(project)
    db.commit()
    db.refresh(project)
    create_project_tree(settings, project.id)
    _register_parties_and_people(db, project.id, body, user)
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


def _register_parties_and_people(db: Session, project_id: int, body: ProjectCreate, user: User) -> None:
    """Стороны компаний и люди нового проекта: заказчик из формы, исполнитель (из формы или от компании автора), сам автор.
    Ошибка здесь не должна ломать создание проекта: реестр можно дополнить позже."""
    import logging

    from app.app.models.company import SIDE_CUSTOMER, SIDE_EXECUTOR
    from app.app.services import people
    from app.app.services import user_profile as profiles

    try:
        customer = (body.customer_company or "").strip()
        executor = (body.executor_company or "").strip()
        if not executor:
            card = profiles.get_profile(db, user.id)
            if card is not None and card.company_id:
                from app.app.models.company import Company

                row = db.get(Company, card.company_id)
                executor = row.name if row is not None and row.side == SIDE_EXECUTOR else ""
        if customer:
            people.set_party(db, project_id, customer, SIDE_CUSTOMER, by_user_id=user.id)
        if executor:
            people.set_party(db, project_id, executor, SIDE_EXECUTOR, by_user_id=user.id)
        people.person_from_user(db, project_id, user.id, by_user_id=user.id)
        for item in body.customer_people:
            people.create_person(db, project_id, item.name, company_name=customer or None, side=SIDE_CUSTOMER, side_source="human",
                                 job_title=(item.job_title or "").strip() or None, authority="approver" if item.can_approve else None,
                                 by_user_id=user.id)
        db.commit()
    except people.PeopleError:
        db.rollback()
        logging.getLogger("skkkk.projects").warning("реестр нового проекта %s не заполнен полностью", project_id, exc_info=True)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    return require_project(db, user, project_id)


@router.patch("/{project_id}", response_model=ProjectOut)
def rename_project(
    project_id: int,
    body: ProjectRename,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    """Новое название проекта из списка проектов; права — как на удаление (автор или админ)."""
    project = require_project(db, user, project_id)
    if not is_project_owner(user, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    name = normalize_project_name(body.name)
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Project name required")
    old = project.name
    if name != old:
        project.name = name
        db.commit()
        db.refresh(project)
        log_activity(
            db,
            user_id=user.id,
            project_id=project.id,
            block=BLOCK_PROJECT,
            action=ACTION_RENAME,
            title="Переименовал проект",
            content=f"{old} → {name}",
            target_kind="project",
            target_id=project.id,
        )
    return project


@router.get("/{project_id}/delete-preview")
def delete_project_preview(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    """Что будет удалено вместе с проектом (пункт 3.10): счётчики для окна подтверждения."""
    from app.app.models.artifact import Artifact
    from app.app.models.entity import UserNote
    from app.app.models.meeting import Meeting

    project = require_project(db, user, project_id)
    if not is_project_owner(user, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    calls = db.scalar(select(func.count()).select_from(Artifact).where(Artifact.project_id == project_id, Artifact.file_type == "audio")) or 0
    docs = db.scalar(select(func.count()).select_from(Artifact).where(Artifact.project_id == project_id, Artifact.file_type != "audio")) or 0
    meetings = db.scalar(select(func.count()).select_from(Meeting).where(Meeting.project_id == project_id)) or 0
    notes = db.scalar(select(func.count()).select_from(UserNote).where(UserNote.project_id == project_id)) or 0
    return {"documents": docs, "calls": calls, "meetings": meetings, "notes": notes}


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
