from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import (
    authenticate_new_user,
    clear_session_cookie,
    create_access_token,
    set_session_cookie,
)
from app.app.logging_setup import log_event
from app.app.models.user import User
from app.app.schemas.auth import LoginRequest, TokenResponse
from app.app.services.user_activity import ACTION_LOGIN, ACTION_LOGOUT, BLOCK_AUTH, log_activity
from app.app.services.login_allowlist import resolve_allowed_login
from app.app.services.user_identity import (
    claim_telegram_id,
    find_users_by_login_name,
    free_telegram_id,
    merge_users_into,
    pick_canonical_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("skkkk.auth")


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    log_event(logger, "login.attempt", name=body.name, telegram_id=body.telegram_id)
    telegram_id = (body.telegram_id or "").strip()
    if not (body.name or "").strip() or not telegram_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name and Telegram ID required")
    allowlist = settings.login_allowlist_path
    name = resolve_allowed_login(body.name, allowlist)
    if name is None:
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
        log_event(
            logger,
            "login.denied",
            name=name,
            telegram_id=telegram_id,
            reason="password matches no role",
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    role, pwd_hash = matched
    matches = find_users_by_login_name(db, name)
    created = False
    if matches:
        user = pick_canonical_user(db, matches) if len(matches) > 1 else matches[0]
        if len(matches) > 1:
            merge_users_into(db, user, [u for u in matches if u.id != user.id])
        prev_role = user.role
        user.name = name
        user.role = role
        user.password_hash = pwd_hash
        claim_telegram_id(db, user, telegram_id)
        db.commit()
        db.refresh(user)
        if prev_role != role:
            log_event(
                logger,
                "user.role_from_password",
                user_id=user.id,
                name=user.name,
                from_role=prev_role,
                to_role=role,
            )
    else:
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
    token = create_access_token(settings, user)
    set_session_cookie(response, request, settings, token)
    log_event(
        logger,
        "login.ok",
        user_id=user.id,
        name=user.name,
        telegram_id=user.telegram_id,
        role=user.role,
        new_user=created,
    )
    log_activity(
        db,
        user_id=user.id,
        block=BLOCK_AUTH,
        action=ACTION_LOGIN,
        title="Вход на сайт" if not created else "Первый вход, аккаунт создан",
        meta={"new_user": created},
    )
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
