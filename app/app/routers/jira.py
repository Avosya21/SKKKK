"""Jira на главной странице: подключение личным токеном, список проектов Jira, загрузка выбранных, обновление и привязка.

Подключить токен может любой вошедший (так выдаётся доступ к уже загруженным проектам); загружать проекты — менеджер и
администратор; влить Jira в существующий проект и снять привязку — только администратор. Документы задач и сводка проекта
отдаются отдельно, ``GET /projects/{id}/jira/exports``, и только администратору. Логика — services/jira_import.py.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import get_current_user, require_role
from app.app.models.user import User
from app.app.services import jira_import
from app.app.services.project_access import require_project, require_project_staff
from app.app.services.user_activity import ACTION_CREATE, BLOCK_PROJECT, log_activity

router = APIRouter(tags=["jira"])


class JiraConnect(BaseModel):
    token: str = Field(min_length=1, max_length=4000)


class JiraPick(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    project_id: int | None = None  # влить в существующий проект портала (только admin); пусто — новый проект


class JiraImport(BaseModel):
    projects: list[JiraPick] = Field(min_length=1, max_length=100)


class JiraLinkIn(BaseModel):
    jira_key: str = Field(min_length=1, max_length=64)


def _fail(exc: Exception, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=str(exc))


@router.get("/jira/status")
def jira_status(db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)],
                user: Annotated[User, Depends(get_current_user)]):
    """Настроена ли Jira на сервере и подключён ли токен пользователя. Сам токен не возвращается никогда."""
    account = jira_import.account_of(db, user.id)
    return {"available": jira_import.server_configured(settings), "connected": account is not None,
            "name": account.jira_name if account else None, "error": account.last_error if account else None,
            "can_import": user.role in ("admin", "manager"), "can_link_existing": user.role == "admin"}


@router.post("/jira/connect")
def jira_connect(body: JiraConnect, db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)],
                 user: Annotated[User, Depends(get_current_user)]):
    try:
        return jira_import.connect(db, settings, user, body.token)
    except jira_import.JiraImportError as exc:
        raise _fail(exc) from exc


@router.delete("/jira/connect", status_code=status.HTTP_204_NO_CONTENT)
def jira_disconnect(db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(get_current_user)]):
    jira_import.disconnect(db, user)


@router.get("/jira/projects")
def jira_projects(db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)],
                  user: Annotated[User, Depends(get_current_user)]):
    """Проекты Jira, которые видит токен пользователя, с отметкой, какие уже есть в портале."""
    try:
        return {"projects": jira_import.visible_projects(db, settings, user)}
    except jira_import.JiraImportError as exc:
        raise _fail(exc) from exc


@router.get("/jira/projects/{jira_key}/count")
def jira_project_count(jira_key: str, db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)],
                       user: Annotated[User, Depends(get_current_user)]):
    """Сколько задач в проекте Jira — чтобы понимать объём до загрузки. Считает токеном пользователя."""
    from app.app.services.jira_client import JiraError

    try:
        with jira_import.client_for(db, settings, user.id) as client:
            return {"key": jira_key.strip().upper(), "issues": client.count(jira_key)}
    except (jira_import.JiraImportError, JiraError) as exc:
        raise _fail(exc) from exc


@router.post("/jira/import")
def jira_import_projects(body: JiraImport, db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)],
                         user: Annotated[User, Depends(require_role("admin", "manager"))]):
    """Загрузить выбранные проекты Jira: создать (или привязать) проекты портала и начать скачивание в фоне."""
    try:
        links = jira_import.import_projects(db, settings, user, [pick.model_dump() for pick in body.projects])
    except jira_import.JiraImportError as exc:
        raise _fail(exc) from exc
    for link in links:
        log_activity(db, user_id=user.id, project_id=link.project_id, block=BLOCK_PROJECT, action=ACTION_CREATE,
                     title="Подключил проект Jira", content=link.jira_key, target_kind="project", target_id=link.project_id)
    jira_import.start_sync(settings, [link.id for link in links], user.id)
    return {"started": [jira_import.link_out(db, link) for link in links]}


@router.get("/projects/{project_id}/jira")
def project_jira(project_id: int, db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(get_current_user)]):
    """Привязки проекта к Jira и ход загрузки."""
    project = require_project(db, user, project_id)
    return {"links": [jira_import.link_out(db, link) for link in jira_import.links_of_project(db, project.id)]}


@router.post("/projects/{project_id}/jira/sync")
def project_jira_sync(project_id: int, db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)],
                      user: Annotated[User, Depends(get_current_user)]):
    """Проверить обновления в Jira сейчас (токеном нажавшего)."""
    project = require_project_staff(db, user, project_id)
    links = jira_import.links_of_project(db, project.id)
    if not links:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Проект не привязан к Jira")
    if jira_import.account_of(db, user.id) is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Jira не подключена: вставьте свой токен на странице проектов")
    for link in links:
        link.status, link.status_text = "queued", "в очереди на загрузку"
    db.commit()
    jira_import.start_sync(settings, [link.id for link in links], user.id)
    return {"started": [jira_import.link_out(db, link) for link in links]}


@router.get("/projects/{project_id}/jira/check")
def project_jira_check(project_id: int, db: Annotated[Session, Depends(get_db)], settings: Annotated[Settings, Depends(get_settings)],
                       user: Annotated[User, Depends(get_current_user)]):
    """Сверка «Jira ↔ портал» (ничего не меняет): все ли задачи и вложения загружены, совпадают ли документы с Jira, всё ли разобрано."""
    from app.app.services.jira_client import JiraError

    project = require_project_staff(db, user, project_id)
    links = jira_import.links_of_project(db, project.id)
    if not links:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Проект не привязан к Jira")
    try:
        with jira_import.client_for(db, settings, user.id) as client:
            reports = [jira_import.check_link(db, settings, link, client) for link in links]
    except (jira_import.JiraImportError, JiraError) as exc:
        raise _fail(exc) from exc
    return {"ok": all(r["ok"] for r in reports), "links": reports}


@router.get("/projects/{project_id}/jira/exports")
def project_jira_exports(project_id: int, db: Annotated[Session, Depends(get_db)],
                         settings: Annotated[Settings, Depends(get_settings)],
                         user: Annotated[User, Depends(get_current_user)]):
    """Документы задач Jira и сводка проекта. В общий список материалов не входят. Видит только администратор."""
    require_project(db, user, project_id)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Выгрузки задач Jira видит только администратор")
    from app.app.models.artifact import Artifact
    from app.app.routers.upload import artifact_outs

    kinds = jira_import.shelf_kinds(db, project_id)
    if not kinds:
        return []
    rows = db.scalars(select(Artifact).where(Artifact.id.in_(kinds)).order_by(Artifact.id.desc())).all()
    return artifact_outs(db, settings, user, list(rows), kinds)


@router.put("/projects/{project_id}/jira/link")
def project_jira_link(project_id: int, body: JiraLinkIn, db: Annotated[Session, Depends(get_db)],
                      user: Annotated[User, Depends(require_role("admin"))]):
    """Привязать проект Jira к существующему проекту портала (таблица соответствий; только admin). Загрузка — отдельно."""
    project = require_project(db, user, project_id)
    try:
        link = jira_import.create_link(db, project, body.jira_key, None, user)
    except jira_import.JiraImportError as exc:
        raise _fail(exc, status.HTTP_409_CONFLICT) from exc
    jira_import.refresh_access_cached(db)
    db.commit()
    return jira_import.link_out(db, link)


@router.delete("/projects/{project_id}/jira/link/{jira_key}", status_code=status.HTTP_204_NO_CONTENT)
def project_jira_unlink(project_id: int, jira_key: str, db: Annotated[Session, Depends(get_db)],
                        user: Annotated[User, Depends(require_role("admin"))]):
    """Снять привязку (только admin). Уже загруженные материалы остаются в проекте."""
    project = require_project(db, user, project_id)
    link = jira_import.link_by_key(db, jira_key)
    if link is None or link.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Привязка не найдена")
    jira_import.unlink(db, link)
