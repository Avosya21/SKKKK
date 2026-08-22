from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.models.user import User
from app.app.services.auth import verify_password

security = HTTPBearer(auto_error=False)


def create_access_token(settings: Settings, user: User) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": str(user.id), "role": user.role, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_token(settings: Settings, token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=["HS256"])


def authenticate_new_user(settings: Settings, password: str) -> tuple[str, str] | None:
    # Хеши ролей могут быть перевыпущены командой `credentials --reset`, пока API
    # уже работает. `settings` закеширован через lru_cache, поэтому для входа
    # читаем свежие хеши прямо из `.env`, чтобы новый пароль принимался сразу.
    #
    # Но в тестах/CI хеши часто задаются через env-переменные, и тогда чтение
    # `.env` может привести к расхождению. Поэтому: если env-переменные уже есть,
    # используем `settings.*` как источник истины.
    import os
    env_keys = {"ADMIN_PASSWORD_HASH", "MANAGER_PASSWORD_HASH", "USER_PASSWORD_HASH"}
    use_fresh_from_env_file = not any(k in os.environ for k in env_keys)

    if use_fresh_from_env_file:
        from app.app.bootstrap import role_hashes

        current = role_hashes()
    else:
        current = {}

    pairs = (
        ("admin", current.get("admin") or settings.admin_password_hash),
        ("manager", current.get("manager") or settings.manager_password_hash),
        ("user", current.get("user") or settings.user_password_hash),
    )
    for role, hashed in pairs:
        if verify_password(password, hashed):
            return role, hashed
    return None


def get_current_user(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> User:
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = decode_token(settings, creds.credentials)
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from None
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def require_role(*roles: str):
    def _dep(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user

    return _dep
