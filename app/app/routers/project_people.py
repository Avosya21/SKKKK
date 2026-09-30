"""Люди и стороны проекта (docs/plans/PLATFORM-FOUNDATION-PLAN.md, ПФ3): реестр людей, «Кто это?», стороны компаний.

Видят и правят админ и менеджер проекта (Д6); каждое действие пишется в журнал правок реестра.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import get_current_user
from app.app.models.person import PeopleLog, Person, ProjectParty, UnknownActor
from app.app.models.project_member import ProjectMember
from app.app.models.speaker_profile import SpeakerProfile
from app.app.models.user import User
from app.app.services import memory_platform, people, voice_link
from app.app.services.project_access import require_project_staff

router = APIRouter(prefix="/projects", tags=["project-people"])


class PersonIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    side: str = "unknown"
    job_title: str | None = Field(default=None, max_length=200)
    authority: str | None = None
    user_id: int | None = None


class PersonPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    side: str | None = None
    job_title: str | None = Field(default=None, max_length=200)
    authority: str | None = None
    clear_authority: bool = False


class MergeIn(BaseModel):
    drop_id: int


class AliasIn(BaseModel):
    alias: str = Field(min_length=1, max_length=200)


class ResolveIn(BaseModel):
    person_id: int | None = None
    new_name: str | None = Field(default=None, max_length=200)
    side: str | None = None
    ignore: bool = False


class PartyIn(BaseModel):
    company_name: str = Field(min_length=1, max_length=200)
    side: str


class ConsentIn(BaseModel):
    given: bool


class FromUserIn(BaseModel):
    user_id: int


def _fail(exc: people.PeopleError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _person_out(db: Session, p: Person, users: dict[int, str], voices: dict[int, int], suggestions: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "id": p.id, "display_name": p.display_name, "aliases": [a.alias for a in people.aliases_of(db, p.id)],
        "company_name": p.company_name, "side": p.side, "side_source": p.side_source, "job_title": p.job_title,
        "authority": p.authority, "user": {"id": p.user_id, "name": users.get(p.user_id, "")} if p.user_id else None,
        "external": p.user_id is None, "voice_suggestions": suggestions.get(p.user_id, []) if p.user_id else [],
        "voices": voices.get(p.id, 0), "voice_consent_at": p.voice_consent_at.isoformat() if p.voice_consent_at else None,
    }


def _payload(db: Session, settings: Settings, project) -> dict[str, Any]:
    pid = project.id
    rows = people.list_people(db, pid)
    user_ids = {p.user_id for p in rows if p.user_id}
    users = {u.id: u.name for u in db.scalars(select(User).where(User.id.in_(user_ids))).all()} if user_ids else {}
    voices = dict(db.execute(select(SpeakerProfile.person_id, func.count()).where(SpeakerProfile.person_id.in_([p.id for p in rows] or [-1]))
                             .group_by(SpeakerProfile.person_id)).all())
    if user_ids:  # у сотрудника голоса считаются по учётке (общие слепки), у внешнего человека — по человеку проекта
        voices.update({p.id: n for p in rows if p.user_id for n in [db.scalar(select(func.count()).select_from(SpeakerProfile).where(SpeakerProfile.user_id == p.user_id))]})
    suggestions: dict[int, list[dict[str, Any]]] = {}
    for s in voice_link.suggestions(db):  # только предложения по точному имени сотрудника с согласием; привязывает человек
        suggestions.setdefault(s.user_id, []).append({"id": s.profile_id, "name": s.voice_name, "scope": s.scope})
    linked = {p.user_id for p in rows if p.user_id}
    free_members = []
    for m in db.scalars(select(ProjectMember).where(ProjectMember.project_id == pid)).all():
        if m.user_id not in linked:
            u = db.get(User, m.user_id)
            free_members.append({"user_id": m.user_id, "name": u.name if u else ""})
    parties = db.scalars(select(ProjectParty).where(ProjectParty.project_id == pid).order_by(ProjectParty.id)).all()
    return {
        "platform_enabled": memory_platform.enabled(settings, project),
        "people": [_person_out(db, p, users, voices, suggestions) for p in rows],
        "unknown_open": len(people.open_unknown(db, pid)),
        "parties": [{"company_name": r.company_name, "side": r.side} for r in parties],
        "members_without_person": free_members,
    }


@router.get("/{project_id}/people")
def list_people(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project_staff(db, user, project_id)
    return _payload(db, settings, project)


@router.post("/{project_id}/people", status_code=status.HTTP_201_CREATED)
def create_person(
    project_id: int, body: PersonIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project_staff(db, user, project_id)
    try:
        row = people.create_person(db, project_id, body.display_name, user_id=body.user_id, company_name=body.company_name,
                                   side=body.side, side_source="human", job_title=body.job_title, authority=body.authority, by_user_id=user.id)
        db.commit()
    except people.PeopleError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {"id": row.id, **_payload(db, settings, project)}


@router.post("/{project_id}/people/from-user", status_code=status.HTTP_201_CREATED)
def person_from_user(
    project_id: int, body: FromUserIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Сотрудник портала -> человек проекта: имя, компания и должность из карточки, сторона от компании проекта или списка."""
    project = require_project_staff(db, user, project_id)
    member = db.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == body.user_id))
    if member is None:
        raise HTTPException(status_code=400, detail="Этот сотрудник не участник проекта")
    try:
        row = people.person_from_user(db, project_id, body.user_id, by_user_id=user.id)
        db.commit()
    except people.PeopleError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return {"id": row.id, **_payload(db, settings, project)}


@router.put("/{project_id}/people/{person_id}")
def update_person(
    project_id: int, person_id: int, body: PersonPatch,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project_staff(db, user, project_id)
    fields: dict[str, Any] = {k: v for k, v in body.model_dump(exclude={"side", "clear_authority"}).items() if v is not None}
    if body.clear_authority:
        fields["authority"] = None
    try:
        if fields:
            people.update_person(db, project_id, person_id, by_user_id=user.id, **fields)
        if body.side is not None:
            people.set_side(db, project_id, person_id, body.side, source="human", by_user_id=user.id, force=True)
        db.commit()
    except people.PeopleError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return _payload(db, settings, project)


@router.post("/{project_id}/people/{person_id}/merge")
def merge_people(
    project_id: int, person_id: int, body: MergeIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project_staff(db, user, project_id)
    try:
        people.merge(db, project_id, person_id, body.drop_id, by_user_id=user.id)
        db.commit()
    except people.PeopleError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return _payload(db, settings, project)


@router.put("/{project_id}/people/{person_id}/voice-consent")
def voice_consent(
    project_id: int, person_id: int, body: ConsentIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Отметить, что внешний человек согласен на запись голоса (Д5). Отзыв согласия — только в консоли (Д4)."""
    project = require_project_staff(db, user, project_id)
    if not body.given:
        raise HTTPException(status_code=400, detail="Отзыв согласия на голос делается только в консоли: scripts/people_voice.py withdraw")
    try:
        people.give_voice_consent(db, project_id, person_id, by_user_id=user.id)
        db.commit()
    except people.PeopleError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return _payload(db, settings, project)


@router.post("/{project_id}/people/{person_id}/voices/{profile_id}/link")
def link_voice(
    project_id: int, person_id: int, profile_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Привязать слепок голоса к учётке сотрудника (щелчок человека; только с согласием в карточке и по предложению по имени)."""
    project = require_project_staff(db, user, project_id)
    try:
        person = people.get(db, project_id, person_id)
        if person.user_id is None:
            raise people.PeopleError("Голос привязывается к учётке сотрудника; у этого человека учётки нет")
        offered = {s.profile_id for s in voice_link.suggestions(db) if s.user_id == person.user_id}
        if profile_id not in offered:
            raise people.PeopleError("Этот голос не подходит по имени сотрудника или уже привязан")
        voice_link.link_profile(db, profile_id, person.user_id)
        people._log(db, project_id, person.id, user.id, "link_voice", None, {"profile_id": profile_id})
        db.commit()
    except (people.PeopleError, voice_link.VoiceLinkError) as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _payload(db, settings, project)


@router.post("/{project_id}/people/{person_id}/aliases")
def add_alias(
    project_id: int, person_id: int, body: AliasIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project_staff(db, user, project_id)
    try:
        people.add_alias(db, project_id, person_id, body.alias, source="human", by_user_id=user.id)
        db.commit()
    except people.PeopleError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return _payload(db, settings, project)


@router.get("/{project_id}/people/unknown")
def list_unknown(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    require_project_staff(db, user, project_id)
    out = []
    for row in people.open_unknown(db, project_id):
        out.append({"id": row.id, "kind": row.kind, "label": row.label, "artifact_id": row.artifact_id,
                    "examples": json.loads(row.examples_json or "[]"), "suggestions": json.loads(row.suggestions_json or "[]")})
    return {"items": out}


@router.post("/{project_id}/people/unknown/{actor_id}/resolve")
def resolve_unknown(
    project_id: int, actor_id: int, body: ResolveIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    """«Это X» (person_id), «Новый человек» (new_name [+ side]) или «Не участник» (ignore)."""
    project = require_project_staff(db, user, project_id)
    try:
        person_id = body.person_id
        if body.new_name and not body.ignore:
            created = people.create_person(db, project_id, body.new_name, side=body.side or "unknown", side_source="human", by_user_id=user.id)
            person_id = created.id
        people.resolve_unknown(db, project_id, actor_id, person_id=person_id, ignore=body.ignore, by_user_id=user.id)
        db.commit()
    except people.PeopleError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return _payload(db, settings, project)


@router.put("/{project_id}/parties")
def set_party(
    project_id: int, body: PartyIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    project = require_project_staff(db, user, project_id)
    try:
        people.set_party(db, project_id, body.company_name, body.side, by_user_id=user.id)
        db.commit()
    except people.PeopleError as exc:
        db.rollback()
        raise _fail(exc) from exc
    return _payload(db, settings, project)


@router.post("/{project_id}/people/seed")
def seed_people(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    """«Найти людей в материалах»: пополняет реестр черновиком без модели; решения человека не трогает."""
    from app.app.services import people_seed

    project = require_project_staff(db, user, project_id)
    report = people_seed.seed_project(db, settings, project_id, apply=True, by_user_id=user.id)
    return {"created": report["created"], "unknown": len(report["unknown"]), **_payload(db, settings, project)}


@router.get("/{project_id}/people/log")
def people_log(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = 50,
):
    require_project_staff(db, user, project_id)
    rows = db.scalars(select(PeopleLog).where(PeopleLog.project_id == project_id).order_by(PeopleLog.id.desc()).limit(max(1, min(limit, 200)))).all()
    names = {}
    out = []
    for r in rows:
        if r.user_id and r.user_id not in names:
            u = db.get(User, r.user_id)
            names[r.user_id] = u.name if u else ""
        out.append({"id": r.id, "action": r.action, "person_id": r.person_id, "by": names.get(r.user_id, ""),
                    "at": r.created_at.isoformat() if r.created_at else None,
                    "before": json.loads(r.before_json) if r.before_json else None, "after": json.loads(r.after_json) if r.after_json else None})
    return {"items": out}


class PlatformIn(BaseModel):
    enabled: bool


@router.put("/{project_id}/people-platform")
def set_platform(
    project_id: int, body: PlatformIn,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Включить или выключить реестр как источник сторон в контракте для проекта (только админ; выключение возвращает прежнюю логику)."""
    project = require_project_staff(db, user, project_id)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Переключает только администратор")
    data = project.settings()
    data["memory_platform"] = bool(body.enabled)
    project.set_settings(data)
    people._log(db, project_id, None, user.id, "platform_on" if body.enabled else "platform_off", None, {"enabled": bool(body.enabled)})
    db.commit()
    from app.app.services import contract as ctr

    ctr._cache.pop(project_id, None)  # стороны в контракте пересчитаются
    return _payload(db, settings, project)
