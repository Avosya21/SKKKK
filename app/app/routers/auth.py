"""Вход и выход.

``POST /auth/login`` — пара логин+пароль (без утечек) → код → сессия/карточка.
``POST /auth/register`` — первый вход по allowlist: логин → код → setup пароля → карточка.
Все роли (включая ``user``) заводят личный пароль при ``PERSONAL_PASSWORD=required``.
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
    VALID_ROLES,
    clear_session_cookie,
    create_access_token,
    get_current_user,
    set_session_cookie,
)
from app.app.logging_setup import log_event
from app.app.models.user import User
from app.app.schemas.auth import (
    CodeRequestBody,
    CodeVerifyBody,
    LoginProbeResponse,
    LoginRequest,
    LoginStepResponse,
    PasswordChangeRequest,
    PasswordStateResponse,
    PersonalSetupRequest,
    ProfileCompanyOut,
    ProfileOptionsResponse,
    RegisterRequest,
    RoleOption,
    TicketRequest,
    TokenResponse,
)
from app.app.services import avatars, b24_auth, email_otp, login_codes, personal_password
from app.app.services import login_roles
from app.app.services import user_profile as profiles
from app.app.services.login_allowlist import resolve_allowed_login
from app.app.services.rate_limit import LOGIN_LIMITER
from app.app.services.user_activity import (
    ACTION_LOGIN,
    ACTION_LOGOUT,
    ACTION_PASSWORD_CHANGE,
    ACTION_PASSWORD_SET,
    ACTION_PROFILE_SET,
    BLOCK_AUTH,
    log_activity,
)
from app.app.services.user_identity import (
    claim_telegram_id,
    find_users_by_login_name,
    free_telegram_id,
    merge_users_into,
    pick_canonical_user,
    placeholder_telegram_id,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("primaryanalitics.auth")

TICKET_MINUTES = 5
CODE_TICKET_MINUTES = 10
PROFILE_TICKET_MINUTES = 15
_TICKET_PURPOSE = "login_step"
_used_tickets: dict[str, float] = {}
_used_lock = threading.Lock()
PENDING_PASSWORD_HASH = "pending"
# Одна фраза на входе — не палит allowlist / наличие / «пароля ещё нет».
LOGIN_BAD_PAIR = "Неверен логин или пароль"
REGISTER_DENIED = "Не удалось начать регистрацию. Проверьте логин или попробуйте позже."
REGISTER_EXISTS = "Данная учетная запись уже зарегистрирована"


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


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _roles_options(name: str, settings: Settings) -> list[RoleOption]:
    return [RoleOption(role=r["role"], label=r["label"]) for r in login_roles.roles_payload(name, settings.login_roles_path)]


def _make_ticket(
    settings: Settings,
    user: User,
    role: str,
    *,
    step: str,
    created: bool,
    channel: str | None = None,
    pwd_ok: bool = False,
    roles: list[str] | None = None,
    role_pending: bool = False,
    register: bool = False,
) -> str:
    if step == "profile":
        minutes = PROFILE_TICKET_MINUTES
    elif step == "code":
        minutes = CODE_TICKET_MINUTES
    else:
        minutes = TICKET_MINUTES
    payload = {
        "purpose": _TICKET_PURPOSE,
        "sub": str(user.id),
        "role": role,
        "step": step,
        "new": created,
        "pwd_ok": bool(pwd_ok),
        "jti": uuid.uuid4().hex,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=minutes),
    }
    if channel:
        payload["channel"] = channel
    if roles:
        payload["roles"] = list(roles)
    if role_pending:
        payload["role_pending"] = True
    if register:
        payload["register"] = True
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def _ticket_roles(claims: dict) -> list[str]:
    raw = claims.get("roles")
    if isinstance(raw, list) and raw:
        return [str(r) for r in raw if str(r) in VALID_ROLES]
    role = claims.get("role")
    return [role] if role in VALID_ROLES else []


def _read_ticket(settings: Settings, db: Session, ticket: str, *steps: str) -> tuple[dict, User]:
    expired = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Время на вход вышло. Войдите заново.",
    )
    try:
        claims = jwt.decode(ticket, settings.secret_key, algorithms=["HS256"])
        user_id = int(claims["sub"])
    except (JWTError, KeyError, ValueError):
        raise expired from None
    if claims.get("purpose") != _TICKET_PURPOSE:
        raise expired
    if not _ticket_roles(claims) and claims.get("role") not in VALID_ROLES:
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


def _ensure_user(
    db: Session,
    *,
    name: str,
    role: str,
    telegram_id: str,
) -> tuple[User, bool]:
    matches = find_users_by_login_name(db, name)
    if matches:
        user = pick_canonical_user(db, matches) if len(matches) > 1 else matches[0]
        if len(matches) > 1:
            merge_users_into(db, user, [u for u in matches if u.id != user.id])
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
        return user, False
    telegram_id = telegram_id or placeholder_telegram_id(name)
    free_telegram_id(db, telegram_id)
    user = User(
        name=name,
        telegram_id=telegram_id,
        role=role,
        password_hash=PENDING_PASSWORD_HASH,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_event(logger, "user.created", user_id=user.id, name=user.name, role=role)
    return user, True


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


def _after_code(
    request: Request,
    response: Response,
    db: Session,
    settings: Settings,
    user: User,
    claims: dict,
) -> TokenResponse | LoginStepResponse:
    """Код принят: без личного пароля — setup (все роли); иначе карточка или сессия."""
    role = claims["role"]
    created = bool(claims.get("new"))
    needs_personal = personal_password.enabled() and (
        claims.get("register") or not claims.get("pwd_ok") or not personal_password.has_password(user.name)
    )
    if needs_personal and not personal_password.has_password(user.name):
        log_event(logger, "login.step", user_id=user.id, name=user.name, role=role, step="setup")
        return LoginStepResponse(
            step="setup",
            ticket=_make_ticket(
                settings,
                user,
                role,
                step="setup",
                created=created,
                pwd_ok=True,
                register=bool(claims.get("register")),
            ),
            name=user.name,
            min_length=personal_password.min_length(),
            role=role,
        )
    return _after_personal_or_customer(request, response, db, settings, user, claims)


def _after_personal_or_customer(
    request: Request,
    response: Response,
    db: Session,
    settings: Settings,
    user: User,
    claims: dict,
) -> TokenResponse | LoginStepResponse:
    role = claims["role"]
    created = bool(claims.get("new"))
    # Режим off (тесты/аварийный откат) — сессия сразу, без карточки (как раньше).
    if personal_password.enabled() and not profiles.is_complete(profiles.get_profile(db, user.id)):
        log_event(logger, "login.step", user_id=user.id, name=user.name, role=role, step="profile")
        return LoginStepResponse(
            step="profile",
            ticket=_make_ticket(
                settings, user, role, step="profile", created=created, pwd_ok=True
            ),
            name=user.name,
            min_length=personal_password.min_length(),
            role=role,
        )
    pv = 0 if not personal_password.enabled() else personal_password.current_version(user.name)
    return _open_session(
        request,
        response,
        db,
        settings,
        user,
        role,
        created=created,
        pv=pv,
        pp=True,
        pf=True,
    )


def _code_channels_for(role: str, settings: Settings) -> list[str]:
    """Каналы кода: B24 и/или email по .env. Для заказчика без B24 остаётся email."""
    channels: list[str] = []
    if b24_auth.enabled(settings) or b24_auth.dev_mode(settings):
        channels.append("b24")
    if email_otp.enabled(settings) or email_otp.dev_mode(settings):
        channels.append("email")
    if not channels:
        # Запас: заказчику логичнее email, остальным — b24 (dev/стенд).
        return ["email"] if login_roles.is_customer_role(role) else ["b24"]
    return channels


def _default_channel(role: str, channels: list[str]) -> str:
    if login_roles.is_customer_role(role) and "email" in channels:
        return "email"
    if "b24" in channels:
        return "b24"
    return channels[0]


def _resolve_login_roles(
    name: str,
    body_role: str | None,
    settings: Settings,
) -> tuple[str, list[str], bool]:
    """Вернуть (role, roles, role_pending)."""
    roles = login_roles.roles_for(name, settings.login_roles_path)
    if body_role and str(body_role).strip():
        role = login_roles.resolve_role(name, body_role, settings.login_roles_path)
        if role is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=LOGIN_BAD_PAIR)
        return role, roles, False
    if len(roles) == 1:
        return roles[0], roles, False
    if len(roles) > 1:
        return roles[0], roles, True
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=LOGIN_BAD_PAIR)


def _code_step_response(
    *,
    settings: Settings,
    user: User,
    role: str,
    roles: list[str],
    role_pending: bool,
    created: bool,
    channel: str,
    channels: list[str],
    pwd_ok: bool,
    register: bool = False,
) -> LoginStepResponse:
    return LoginStepResponse(
        step="code",
        ticket=_make_ticket(
            settings,
            user,
            role,
            step="code",
            created=created,
            channel=channel,
            pwd_ok=pwd_ok,
            roles=roles,
            role_pending=role_pending,
            register=register,
        ),
        name=user.name,
        min_length=personal_password.min_length(),
        role=None if role_pending else role,
        roles=[RoleOption(role=r, label=login_roles.role_label(r)) for r in roles],
        channel=channel,
        channels=channels,
        code_hint=_code_hint(channel),
    )


def _code_hint(channel: str) -> str:
    if channel == "email":
        return "Код отправлен на вашу электронную почту"
    return "Код отправлен в чат Битрикс24"


@router.get("/login/probe", response_model=LoginProbeResponse)
def login_probe(
    name: str,
    settings: Annotated[Settings, Depends(get_settings)],
):
    """Устарело для UI старта (пароль всегда на форме). Оставлено для совместимости."""
    allowed = resolve_allowed_login(name, settings.login_allowlist_path)
    if allowed is None:
        return LoginProbeResponse(name=(name or "").strip(), roles=[], needs_personal_password=False, is_customer=False)
    roles = _roles_options(allowed, settings)
    only = roles[0].role if len(roles) == 1 else None
    customer = bool(only and login_roles.is_customer_role(only))
    has = personal_password.enabled() and personal_password.has_password(allowed)
    return LoginProbeResponse(
        name=allowed,
        roles=roles,
        needs_personal_password=has,
        is_customer=customer or any(login_roles.is_customer_role(r.role) for r in roles),
    )


@router.post("/login", response_model=TokenResponse | LoginStepResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    """Вход зарегистрированного: только проверка пары логин+пароль, без утечек."""
    log_event(logger, "login.attempt", name=body.name, telegram_id=body.telegram_id)
    ip_key = _ip_key(request)
    name_key = _name_key(body.name or "")
    _throttle(ip_key, name_key)

    telegram_id = (body.telegram_id or "").strip()
    if not (body.name or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name required")

    def _deny(reason: str) -> None:
        LOGIN_LIMITER.fail(ip_key)
        LOGIN_LIMITER.fail(name_key)
        log_event(logger, "login.denied", name=(body.name or "").strip(), reason=reason)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=LOGIN_BAD_PAIR)

    name = resolve_allowed_login(body.name, settings.login_allowlist_path)
    if name is None:
        _deny("not on allowlist")

    try:
        role, roles, role_pending = _resolve_login_roles(name, body.role, settings)
    except HTTPException:
        LOGIN_LIMITER.fail(ip_key)
        LOGIN_LIMITER.fail(name_key)
        raise

    plain = (body.password or "").strip()
    if personal_password.enabled():
        if not personal_password.has_password(name) or not plain or not personal_password.verify(name, plain):
            _deny("bad pair")
        pwd_ok = True
    else:
        pwd_ok = True

    user, created = _ensure_user(db, name=name, role=role, telegram_id=telegram_id)
    channels = _code_channels_for(role, settings)
    default_channel = _default_channel(role, channels)
    log_event(
        logger,
        "login.step",
        user_id=user.id,
        name=user.name,
        role=role,
        step="code",
        role_pending=role_pending,
    )
    return _code_step_response(
        settings=settings,
        user=user,
        role=role,
        roles=roles,
        role_pending=role_pending,
        created=created,
        channel=default_channel,
        channels=channels,
        pwd_ok=pwd_ok,
        register=False,
    )


@router.post("/register", response_model=LoginStepResponse)
def register(
    body: RegisterRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    """Первый вход: только логины из allowlist без личного пароля. Порядок: код → пароль."""
    log_event(logger, "register.attempt", name=body.name)
    ip_key = _ip_key(request)
    name_key = _name_key(body.name or "")
    _throttle(ip_key, name_key)

    if not (body.name or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name required")
    if not personal_password.enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Сейчас регистрация не нужна — войдите логином.",
        )

    name = resolve_allowed_login(body.name, settings.login_allowlist_path)
    if name is None:
        LOGIN_LIMITER.fail(ip_key)
        log_event(logger, "register.denied", name=(body.name or "").strip(), reason="not on allowlist")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=REGISTER_DENIED)

    if personal_password.has_password(name):
        LOGIN_LIMITER.fail(ip_key)
        log_event(logger, "register.denied", name=name, reason="already registered")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=REGISTER_EXISTS)

    try:
        role, roles, role_pending = _resolve_login_roles(name, None, settings)
    except HTTPException:
        LOGIN_LIMITER.fail(ip_key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=REGISTER_DENIED) from None

    user, created = _ensure_user(db, name=name, role=role, telegram_id="")
    channels = _code_channels_for(role, settings)
    default_channel = _default_channel(role, channels)
    log_event(
        logger,
        "login.step",
        user_id=user.id,
        name=user.name,
        role=role,
        step="code",
        register=True,
        role_pending=role_pending,
    )
    return _code_step_response(
        settings=settings,
        user=user,
        role=role,
        roles=roles,
        role_pending=role_pending,
        created=created,
        channel=default_channel,
        channels=channels,
        pwd_ok=False,
        register=True,
    )


@router.post("/login/code/request", response_model=LoginStepResponse)
def login_code_request(
    body: CodeRequestBody,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    claims, user = _read_ticket(settings, db, body.ticket, "code")
    ip_key, name_key = _ip_key(request), _name_key(user.name)
    _throttle(ip_key, name_key)
    role = claims.get("role") if not claims.get("role_pending") else (_ticket_roles(claims) or ["manager"])[0]
    channel = (body.channel or "").strip().casefold()
    allowed = _code_channels_for(role, settings)
    if channel not in allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Канал недоступен")

    ip = _client_ip(request)
    if channel == "b24":
        ok, err = b24_auth.request_code(db, login=user.name, ip=ip, settings=settings)
    else:
        ok, err = email_otp.request_code(db, login=user.name, ip=ip, settings=settings)
    if not ok:
        LOGIN_LIMITER.fail(ip_key)
        log_event(logger, "login.code.fail", user_id=user.id, name=user.name, channel=channel, error=err)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err or login_codes.GENERIC_DENY)

    # Тот же ticket остаётся валидным (не сжигаем) — можно запросить код повторно / сменить канал.
    log_event(logger, "login.code.sent", user_id=user.id, name=user.name, channel=channel)
    avail = _ticket_roles(claims)
    return LoginStepResponse(
        step="code",
        ticket=body.ticket,
        name=user.name,
        min_length=personal_password.min_length(),
        role=None if claims.get("role_pending") else role,
        roles=[RoleOption(role=r, label=login_roles.role_label(r)) for r in avail],
        channel=channel,
        channels=allowed,
        code_hint=_code_hint(channel),
    )


@router.post("/login/code/verify", response_model=TokenResponse | LoginStepResponse)
def login_code_verify(
    body: CodeVerifyBody,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    claims, user = _read_ticket(settings, db, body.ticket, "code")
    ip_key, name_key = _ip_key(request), _name_key(user.name)
    _throttle(ip_key, name_key)
    avail = _ticket_roles(claims)
    if claims.get("role_pending") or len(avail) > 1:
        chosen = login_roles.resolve_role(user.name, body.role, settings.login_roles_path)
        if chosen is None or chosen not in avail:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Выберите роль для входа")
        claims = {**claims, "role": chosen, "role_pending": False}
    elif body.role and str(body.role).strip():
        chosen = str(body.role).strip().casefold()
        if chosen not in avail:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Роль недоступна для этого логина")
        claims = {**claims, "role": chosen}
    result = login_codes.verify(db, login=user.name, code=body.code, settings=settings)
    if not result.ok:
        LOGIN_LIMITER.fail(ip_key)
        LOGIN_LIMITER.fail(name_key)
        log_event(logger, "login.code.denied", user_id=user.id, name=user.name)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=result.error or login_codes.GENERIC_BAD)
    _burn_ticket(claims)
    LOGIN_LIMITER.reset(name_key)
    return _after_code(request, response, db, settings, user, claims)


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
    claims = {**claims, "pwd_ok": True}
    return _after_personal_or_customer(request, response, db, settings, user, claims)


@router.post("/login/profile/options", response_model=ProfileOptionsResponse)
def login_profile_options(
    body: TicketRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
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
    role = claims["role"]
    pv = 0 if not personal_password.enabled() else personal_password.current_version(user.name)
    return _open_session(
        request,
        response,
        db,
        settings,
        user,
        role,
        created=bool(claims.get("new")),
        pv=pv,
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
    if not personal_password.enabled():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Личные пароли выключены")
    if login_roles.is_customer_role(user.role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Для роли заказчика личный пароль не используется")
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
