"""Выдача доступа к проекту: роли manager и user, без admin."""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.project import Project
from app.app.models.project_member import ProjectMember
from app.app.models.user import User
from app.app.services.login_allowlist import load_allowlist, resolve_allowed_login
from app.app.services.project_access import can_grant_access, project_role, require_project
from app.app.services.user_activity import ACTION_CREATE, ACTION_DELETE, BLOCK_PROJECT, log_activity
from app.app.services.user_identity import find_users_by_login_name, login_name_key, placeholder_telegram_id

MEMBER_ROLES = ("manager", "user")
ROLE_LABELS = {"manager": "Менеджер", "user": "Участник"}


def require_grant_access(db: Session, user: User, project_id: int) -> Project:
    project = require_project(db, user, project_id)
    if not can_grant_access(db, user, project):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return project


def _user_name(db: Session, user_id: int) -> str:
    row = db.get(User, user_id)
    return row.name if row else f"id {user_id}"


def _pending_telegram_id(login: str) -> str:
    return placeholder_telegram_id(login)


def ensure_allowlist_user(db: Session, settings: Settings, login: str) -> User:
    display = resolve_allowed_login(login, settings.login_allowlist_path)
    if display is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Login is not on the allowlist")
    matches = find_users_by_login_name(db, display)
    if matches:
        return matches[0]
    user = User(
        name=display,
        telegram_id=_pending_telegram_id(display),
        role="user",
        password_hash=settings.user_password_hash or "pending",
    )
    db.add(user)
    db.flush()
    return user


def list_members_payload(db: Session, project: Project, settings: Settings) -> dict:
    members = db.scalars(
        select(ProjectMember).where(ProjectMember.project_id == project.id).order_by(ProjectMember.id.asc())
    ).all()
    skip_keys = set()
    owner = db.get(User, project.created_by) if project.created_by else None
    if owner:
        skip_keys.add(login_name_key(owner.name))
    for row in members:
        skip_keys.add(login_name_key(_user_name(db, row.user_id)))
    allowlist = load_allowlist(settings.login_allowlist_path)
    candidates = []
    for key, display in sorted(allowlist.items(), key=lambda item: item[1].casefold()):
        if key in skip_keys:
            continue
        existing = find_users_by_login_name(db, display)
        user = existing[0] if existing else None
        candidates.append(
            {
                "login": display,
                "name": user.name if user else display,
                "user_id": user.id if user else None,
            }
        )
    return {
        "members": [
            {
                "user_id": row.user_id,
                "name": _user_name(db, row.user_id),
                "role": row.role,
                "role_label": ROLE_LABELS.get(row.role, row.role),
                "granted_by": row.granted_by,
                "granted_by_name": _user_name(db, row.granted_by),
            }
            for row in members
        ],
        "candidates": candidates,
        "roles": [{"id": role, "label": ROLE_LABELS[role]} for role in MEMBER_ROLES],
    }


def upsert_member(
    db: Session,
    *,
    project: Project,
    actor: User,
    settings: Settings,
    role: str,
    user_id: int | None = None,
    login: str | None = None,
    last_name: str | None = None,
    first_name: str | None = None,
    company_id: int | None = None,
) -> ProjectMember:
    role = (role or "").strip()
    if role not in MEMBER_ROLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Role must be manager or user")
    target: User | None = None
    if login and login.strip():
        target = ensure_allowlist_user(db, settings, login)
    elif user_id:
        target = db.get(User, user_id)
        if target and resolve_allowed_login(target.name, settings.login_allowlist_path) is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Login is not on the allowlist")
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.id == project.created_by:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Owner already has access")
    if target.id == actor.id and project_role(db, actor, project) != "admin" and project.created_by != actor.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot change own access")
    row = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == target.id,
        )
    )
    created = row is None
    if created:
        _ensure_card(db, target, last_name=last_name, first_name=first_name, company_id=company_id)
    if row is None:
        row = ProjectMember(
            project_id=project.id,
            user_id=target.id,
            role=role,
            granted_by=actor.id,
        )
        db.add(row)
    else:
        row.role = role
        row.granted_by = actor.id
    db.commit()
    db.refresh(row)
    _ensure_member_person(db, project.id, target.id, actor.id)
    log_activity(
        db,
        user_id=actor.id,
        project_id=project.id,
        block=BLOCK_PROJECT,
        action=ACTION_CREATE,
        title="Выдал доступ" if created else "Изменил роль доступа",
        content=f"{target.name}: {ROLE_LABELS[role]}",
        target_kind="user",
        target_id=target.id,
    )
    return row


def _ensure_card(db: Session, target: User, *, last_name: str | None, first_name: str | None, company_id: int | None) -> None:
    """Р6 (PEOPLE-FLOW-PLAN, ЛГ3): без карточки (фамилия, имя, компания) учётку в проект не добавить. Поля из формы
    выдачи доступа заполняют недостающее; ничего не передали — 400 с кодом ``card_required`` для формы."""
    from app.app.services import user_profile as profiles

    card = profiles.get_profile(db, target.id)
    if profiles.has_card(card):
        return
    if not (last_name or first_name or company_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={
            "code": "card_required",
            "message": f"У «{target.name}» нет карточки сотрудника. Укажите фамилию, имя и компанию — "
                       "фото и должность он заполнит сам при первом входе.",
            "last_name": (card.last_name if card else "") or "", "first_name": (card.first_name if card else "") or "",
            "company_id": card.company_id if card else None,
        })
    try:
        profiles.fill_card_for_member(db, target.id, last_name=last_name or "", first_name=first_name or "", company_id=company_id)
    except profiles.ProfileError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _ensure_member_person(db: Session, project_id: int, user_id: int, actor_id: int) -> None:
    """ЛГ3: участник проекта сразу человек проекта («Люди и стороны»). Сбой реестра не отменяет выданный доступ."""
    import logging

    from app.app.services import people

    try:
        people.member_person(db, project_id, user_id, by_user_id=actor_id)
        db.commit()
    except people.PeopleError:
        db.rollback()
        logging.getLogger("skkkk.members").warning("участник %s проекта %s не записан в реестр", user_id, project_id, exc_info=True)


def remove_member(db: Session, *, project: Project, actor: User, user_id: int) -> None:
    if user_id == actor.id and project_role(db, actor, project) != "admin" and project.created_by != actor.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove own access")
    row = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == user_id,
        )
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    name = _user_name(db, user_id)
    db.delete(row)
    db.commit()
    log_activity(
        db,
        user_id=actor.id,
        project_id=project.id,
        block=BLOCK_PROJECT,
        action=ACTION_DELETE,
        title="Забрал доступ",
        content=name,
        target_kind="user",
        target_id=user_id,
    )
