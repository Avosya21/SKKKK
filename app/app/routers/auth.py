from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.dependencies import authenticate_new_user, create_access_token
from app.app.logging_setup import log_event
from app.app.models.user import User
from app.app.schemas.auth import LoginRequest, TokenResponse
from app.app.services.auth import hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("skkkk.auth")


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    log_event(logger, "login.attempt", name=body.name, telegram_id=body.telegram_id)
    user = db.scalar(select(User).where(User.telegram_id == body.telegram_id))
    created = False
    if user:
        if not verify_password(body.password, user.password_hash):
            log_event(
                logger,
                "login.denied",
                telegram_id=body.telegram_id,
                role=user.role,
                reason="password does not match stored hash",
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    else:
        matched = authenticate_new_user(settings, body.password)
        if not matched:
            log_event(
                logger,
                "login.denied",
                telegram_id=body.telegram_id,
                reason="password matches no role",
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
        role, pwd_hash = matched
        user = User(
            name=body.name,
            telegram_id=body.telegram_id,
            role=role,
            password_hash=pwd_hash,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        created = True
        log_event(logger, "user.created", user_id=user.id, name=user.name, role=user.role)
    token = create_access_token(settings, user)
    log_event(
        logger,
        "login.ok",
        user_id=user.id,
        name=user.name,
        telegram_id=user.telegram_id,
        role=user.role,
        new_user=created,
    )
    return TokenResponse(access_token=token, role=user.role, name=user.name)
