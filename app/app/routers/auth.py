"""Вход и выход: `/auth/login` (роль определяется паролем), `/auth/logout`.

При `PERSONAL_PASSWORD=required` вход по шагам: `/auth/login` проверяет пароль роли и выдаёт
пропуск (ticket) на шаг 2 — `/auth/login/personal` или `/auth/login/setup`; если карточка не
заполнена — пропуск на шаг 3 `/auth/login/profile` (форма: `/auth/login/profile/options`).
Сессия выдаётся только после всех шагов. Смена своего пароля — `/auth/password/change`.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import (
    authenticate_new_user,
    clear_session_cookie,
    create_access_token,
    get_current_user,
    set_session_cookie,
)
from app.app.logging_setup import log_event
from app.app.models.user import User
from app.app.schemas.auth import (
    LoginRequest,
    LoginStepResponse,
    PasswordChangeRequest,
    PasswordStateResponse,
    PersonalLoginRequest,
    PersonalSetupRequest,
    ProfileCompanyOut,
    ProfileOptionsResponse,
    TicketRequest,
    TokenResponse,
)
from app.app.services import avatars, personal_password
from app.app.services import user_profile as profiles
from app.app.services.user_activity import (
    ACTION_LOGIN,
    ACTION_LOGOUT,
    ACTION_PASSWORD_CHANGE,
    ACTION_PASSWORD_SET,
    ACTION_PROFILE_SET,
    BLOCK_AUTH,
    log_activity,
)
from app.app.services.login_allowlist import resolve_allowed_login
from app.app.services.rate_limit import LOGIN_LIMITER
from app.app.services.user_identity import (
    claim_telegram_id,
    find_users_by_login_name,
    free_telegram_id,
    merge_users_into,
    pick_canonical_user,
    placeholder_telegram_id,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("skkkk.auth")


TICKET_MINUTES = 5
PROFILE_TICKET_MINUTES = 15  # фото и ввод занимают время
_TICKET_PURPOSE = "login_step"
# Использованные пропуска (jti → срок): пропуск одноразовый. В памяти процесса — как и лимитер.
_used_tickets: dict[str, float] = {}
_used_lock = threading.Lock()


def _ip_key(request: Request) -> str:
    return "ip:" + (request.client.host if request.client else "?")


def _name_key(name: str) -> str:
    return "name:" + (name or "").strip().casefold()


def _throttle(*keys: str) -> None:
    wait = max(LOGIN_LIMITER.retry_after(k) for k in keys)
    if wait:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
            headers={"Retry-After": str(wait)},
        )


def _make_ticket(settings: Settings, user: User, role: str, *, step: str, created: bool) -> str:
    minutes = PROFILE_TICKET_MINUTES if step == "profile" else TICKET_MINUTES
    payload = {
        "purpose": _TICKET_PURPOSE,
        "sub": str(user.id),
        "role": role,
        "step": step,
        "new": created,
        "jti": uuid.uuid4().hex,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=minutes),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def _read_ticket(settings: Settings, db: Session, ticket: str, *steps: str) -> tuple[dict, User]:
    """Проверить пропуск; `steps` — для каких шагов он годится (пропуск шага 3 не открывает шаг 2)."""
    expired = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Время на вход вышло. Войдите заново.",
    )
    try:
        claims = jwt.decode(ticket, settings.secret_key, algorithms=["HS256"])
        user_id = int(claims["sub"])
    except (JWTError, KeyError, ValueError):
        raise expired from None
    if claims.get("purpose") != _TICKET_PURPOSE or claims.get("role") not in {"admin", "manager", "user"}:
        raise expired
    if steps and claims.get("step") not in steps:
        raise expired
    now = time.time()
    with _used_lock:
        for jti in [j for j, exp in _used_tickets.items() if exp < now]:
            _used_tickets.pop(jti, None)
        if claims.get("jti") in _used_tickets:
            raise expired
    user = db.get(User, user_id)
    if user is None:
        raise expired
    return claims, user


def _burn_ticket(claims: dict) -> None:
    with _used_lock:
        _used_tickets[str(claims.get("jti"))] = float(claims.get("exp") or time.time() + TICKET_MINUTES * 60)


def _open_session(
    request: Request,
    response: Response,
    db: Session,
    settings: Settings,
    user: User,
    role: str,
    *,
    created: bool,
    pv: int | None = None,
    pp: bool | None = None,
    pf: bool | None = None,
) -> TokenResponse:
    token = create_access_token(settings, user, role, pv=pv, pp=pp, pf=pf)
    set_session_cookie(response, request, settings, token)
    log_event(
        logger,
        "login.ok",
        user_id=user.id,
        name=user.name,
        telegram_id=user.telegram_id,
        role=role,
        new_user=created,
        personal=pp,
    )
    log_activity(
        db,
        user_id=user.id,
        block=BLOCK_AUTH,
        action=ACTION_LOGIN,
        title="Вход на сайт" if not created else "Первый вход, аккаунт создан",
        meta={"new_user": created},
    )
    return TokenResponse(access_token=token, role=role, name=user.name)


def _after_personal(
    request: Request,
    response: Response,
    db: Session,
    settings: Settings,
    user: User,
    claims: dict,
) -> TokenResponse | LoginStepResponse:
    """Личный пароль пройден: карточка не заполнена — пропуск на шаг 3, иначе сессия."""
    if not profiles.is_complete(profiles.get_profile(db, user.id)):
        log_event(logger, "login.step", user_id=user.id, name=user.name, role=claims["role"], step="profile")
        return LoginStepResponse(
            step="profile",
            ticket=_make_ticket(settings, user, claims["role"], step="profile", created=bool(claims.get("new"))),
            name=user.name,
            min_length=personal_password.min_length(),
        )
    return _open_session(
        request,
        response,
        db,
        settings,
        user,
        claims["role"],
        created=bool(claims.get("new")),
        pv=personal_password.current_version(user.name),
        pp=True,
        pf=True,
    )


@router.post("/login", response_model=TokenResponse | LoginStepResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    log_event(logger, "login.attempt", name=body.name, telegram_id=body.telegram_id)
    ip_key = "ip:" + (request.client.host if request.client else "?")
    name_key = "name:" + (body.name or "").strip().casefold()
    wait = max(LOGIN_LIMITER.retry_after(ip_key), LOGIN_LIMITER.retry_after(name_key))
    if wait:
        log_event(logger, "login.throttled", name=(body.name or "").strip(), ip=ip_key, retry_after=wait)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
            headers={"Retry-After": str(wait)},
        )
    # Telegram ID необязателен (поле на форме скрыто, 2026-09-28): указан — пишется на учётку, нет — остаётся прежний
    telegram_id = (body.telegram_id or "").strip()
    if not (body.name or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name required")
    allowlist = settings.login_allowlist_path
    name = resolve_allowed_login(body.name, allowlist)
    if name is None:
        LOGIN_LIMITER.fail(ip_key)
        log_event(
            logger,
            "login.denied",
            name=(body.name or "").strip(),
            telegram_id=telegram_id,
            reason="not on allowlist",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Этот логин не в списке допущенных",
        )
    matched = authenticate_new_user(settings, body.password)
    if not matched:
        LOGIN_LIMITER.fail(ip_key)
        LOGIN_LIMITER.fail(name_key)
        log_event(
            logger,
            "login.denied",
            name=name,
            telegram_id=telegram_id,
            reason="password matches no role",
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    role, pwd_hash = matched
    if not personal_password.enabled():
        # С личным паролем счётчик имени сбрасывает только полный вход: иначе знающий пароль
        # роли обнулял бы его перед каждой серией попыток подобрать личный пароль.
        LOGIN_LIMITER.reset(name_key)
    matches = find_users_by_login_name(db, name)
    created = False
    if matches:
        user = pick_canonical_user(db, matches) if len(matches) > 1 else matches[0]
        if len(matches) > 1:
            merge_users_into(db, user, [u for u in matches if u.id != user.id])
        # Роль и хеш общей учётки не перезаписываем: роль сессии живёт в токене.
        user.name = name
        claim_telegram_id(db, user, telegram_id)
        db.commit()
        db.refresh(user)
        if user.role != role:
            log_event(
                logger,
                "user.session_role",
                user_id=user.id,
                name=user.name,
                account_role=user.role,
                session_role=role,
            )
    else:
        telegram_id = telegram_id or placeholder_telegram_id(name)  # столбец обязательный и уникальный
        free_telegram_id(db, telegram_id)
        user = User(
            name=name,
            telegram_id=telegram_id,
            role=role,
            password_hash=pwd_hash,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        created = True
        log_event(logger, "user.created", user_id=user.id, name=user.name, role=user.role)
    if personal_password.enabled():
        step = "personal" if personal_password.has_password(user.name) else "setup"
        log_event(logger, "login.step", user_id=user.id, name=user.name, role=role, step=step)
        return LoginStepResponse(
            step=step,
            ticket=_make_ticket(settings, user, role, step=step, created=created),
            name=user.name,
            min_length=personal_password.min_length(),
        )
    return _open_session(request, response, db, settings, user, role, created=created)


@router.post("/login/personal", response_model=TokenResponse | LoginStepResponse)
def login_personal(
    body: PersonalLoginRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    claims, user = _read_ticket(settings, db, body.ticket, "personal")
    ip_key, name_key = _ip_key(request), _name_key(user.name)
    _throttle(ip_key, name_key)
    if not personal_password.has_password(user.name):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Личный пароль ещё не создан. Войдите заново.")
    if not personal_password.verify(user.name, body.password):
        LOGIN_LIMITER.fail(ip_key)
        LOGIN_LIMITER.fail(name_key)
        log_event(logger, "personal.denied", user_id=user.id, name=user.name)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный личный пароль")
    _burn_ticket(claims)
    LOGIN_LIMITER.reset(name_key)
    return _after_personal(request, response, db, settings, user, claims)


@router.post("/login/setup", response_model=TokenResponse | LoginStepResponse)
def login_setup(
    body: PersonalSetupRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    claims, user = _read_ticket(settings, db, body.ticket, "setup")
    _throttle(_ip_key(request), _name_key(user.name))
    try:
        personal_password.set_initial(user.name, body.password, body.password_confirm)
    except personal_password.PasswordRuleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except personal_password.PasswordStateError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Личный пароль уже создан. Войдите заново.",
        ) from None
    _burn_ticket(claims)
    LOGIN_LIMITER.reset(_name_key(user.name))
    log_event(logger, "personal.set", user_id=user.id, name=user.name)
    log_activity(db, user_id=user.id, block=BLOCK_AUTH, action=ACTION_PASSWORD_SET, title="Создан личный пароль")
    return _after_personal(request, response, db, settings, user, claims)


@router.post("/login/profile/options", response_model=ProfileOptionsResponse)
def login_profile_options(
    body: TicketRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    """Что показать в форме карточки. Пропуск — в теле запроса, не в адресе."""
    from app.app.services.companies import active_companies

    _claims, user = _read_ticket(settings, db, body.ticket, "profile")
    profile = profiles.get_profile(db, user.id)
    locked = bool(profile and profile.company_id)
    return ProfileOptionsResponse(
        name=user.name,
        companies=[ProfileCompanyOut(id=c.id, name=c.name) for c in active_companies(db)],
        company_locked=locked,
        company_id=profile.company_id if locked else None,
        consent_text=profiles.CONSENT_TEXT,
        consent_details=profiles.CONSENT_DETAILS,
        consent_version=profiles.CONSENT_TEXT_VERSION,
        max_photo_mb=avatars.MAX_BYTES // (1024 * 1024),
    )


@router.post("/login/profile", response_model=TokenResponse)
async def login_profile(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    ticket: Annotated[str, Form(max_length=2048)],
    last_name: Annotated[str, Form(max_length=200)] = "",
    first_name: Annotated[str, Form(max_length=200)] = "",
    job_title: Annotated[str, Form(max_length=300)] = "",
    company_id: Annotated[int | None, Form()] = None,
    avatar: Annotated[UploadFile | None, File()] = None,
):
    """Шаг 3: карточка заполняется разом (поля + фото). Сохранение = согласие на голос (уведомление)."""
    claims, user = _read_ticket(settings, db, ticket, "profile")
    _throttle(_ip_key(request))
    data = await avatar.read(avatars.MAX_BYTES + 1) if avatar is not None else b""
    try:
        profiles.complete_onboarding(
            db,
            user,
            last_name=last_name,
            first_name=first_name,
            job_title=job_title,
            company_id=company_id,
            avatar_bytes=data,
        )
    except profiles.ProfileError as exc:
        code = 413 if exc.field == "avatar" and len(data) > avatars.MAX_BYTES else 400
        return JSONResponse(status_code=code, content={"detail": str(exc), "field": exc.field})
    _burn_ticket(claims)
    log_event(logger, "profile.completed", user_id=user.id, name=user.name)
    log_activity(db, user_id=user.id, block=BLOCK_AUTH, action=ACTION_PROFILE_SET, title="Заполнена карточка")
    return _open_session(
        request,
        response,
        db,
        settings,
        user,
        claims["role"],
        created=bool(claims.get("new")),
        pv=personal_password.current_version(user.name),
        pp=True,
        pf=True,
    )


@router.get("/password/state", response_model=PasswordStateResponse)
def password_state(user: Annotated[User, Depends(get_current_user)]):
    return PasswordStateResponse(
        mode=personal_password.mode(),
        has_password=personal_password.enabled() and personal_password.has_password(user.name),
        min_length=personal_password.min_length(),
    )


@router.post("/password/change", response_model=TokenResponse)
def password_change(
    body: PasswordChangeRequest,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    """Создать (если ещё нет) или сменить свой личный пароль; сессии на других устройствах закрываются."""
    if not personal_password.enabled():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Личные пароли выключены")
    ip_key, name_key = _ip_key(request), _name_key(user.name)
    _throttle(ip_key, name_key)
    had = personal_password.has_password(user.name)
    try:
        if had:
            version = personal_password.change(user.name, body.current_password, body.password, body.password_confirm)
        else:
            version = personal_password.set_initial(user.name, body.password, body.password_confirm)
    except personal_password.PasswordRuleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except personal_password.PasswordStateError as exc:
        # Неверный текущий пароль — это тоже попытка подбора.
        LOGIN_LIMITER.fail(ip_key)
        LOGIN_LIMITER.fail(name_key)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    log_event(logger, "personal.changed" if had else "personal.set", user_id=user.id, name=user.name)
    log_activity(
        db,
        user_id=user.id,
        block=BLOCK_AUTH,
        action=ACTION_PASSWORD_CHANGE if had else ACTION_PASSWORD_SET,
        title="Изменён личный пароль" if had else "Создан личный пароль",
    )
    complete = profiles.is_complete(profiles.get_profile(db, user.id))
    token = create_access_token(settings, user, user.role, pv=version, pp=True, pf=complete)
    set_session_cookie(response, request, settings, token)
    return TokenResponse(access_token=token, role=user.role, name=user.name)


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    from app.app.dependencies import SESSION_COOKIE_NAME, _user_from_access_token

    try:
        token = None
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
        if not token:
            token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            user = _user_from_access_token(db, settings, token)
            log_activity(
                db,
                user_id=user.id,
                block=BLOCK_AUTH,
                action=ACTION_LOGOUT,
                title="Выход с сайта",
            )
    except HTTPException:
        pass
    except Exception:
        logger.exception("logout.activity_failed")
    clear_session_cookie(response)
    return {"ok": True}
