from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import event
from sqlalchemy.orm import Session, object_session
from sqlalchemy.orm.attributes import set_committed_value

from app.app.config import Settings, get_settings
from app.app.db import get_db
from app.app.models.user import User
from app.app.services.auth import verify_password

security = HTTPBearer(auto_error=False)

SESSION_COOKIE_NAME = "primary_analitics_session"


VALID_ROLES = frozenset({"admin", "manager", "user"})


def create_access_token(
    settings: Settings,
    user: User,
    role: str | None = None,
    *,
    pv: int | None = None,
    pp: bool | None = None,
    pf: bool | None = None,
) -> str:
    """Роль сессии — из пароля при входе; в БД (общая учётка) она не перезаписывается.

    `pv` — версия личного пароля логина на момент входа, `pp` — личный пароль пройден,
    `pf` — карточка заполнена (см. `_check_session_gates`).
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": str(user.id), "role": role or user.role, "exp": expire}
    if pv is not None:
        payload["pv"] = int(pv)
    if pp is not None:
        payload["pp"] = bool(pp)
    if pf is not None:
        payload["pf"] = bool(pf)
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def decode_token(settings: Settings, token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=["HS256"])


def _cookie_secure(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return proto == "https"


def set_session_cookie(response: Response, request: Request, settings: Settings, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=int(settings.jwt_expire_minutes) * 60,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


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


def _check_session_gates(settings: Settings, user: User, payload: dict) -> None:
    """Сессия действительна, только пока версия личного пароля логина не менялась.

    Смена или сброс личного пароля увеличивает версию — старые сессии логина получают 401.
    В режиме `required` сессия без пройденного личного пароля (`pp`) или без заполненной
    карточки (`pf`) тоже недействительна: человек уходит на вход и проходит недостающие шаги.
    """
    if settings.personal_password == "off":
        return
    from app.app.services.personal_password import current_version

    stale = int(payload.get("pv") or 0) != current_version(user.name)
    required = settings.personal_password == "required"
    if stale or (required and not (payload.get("pp") and payload.get("pf"))):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    if required:
        # Админ сбросил карточку — открытые сессии человека закрываются сразу, а не через 30 дней.
        from app.app.services.user_profile import get_profile, is_complete

        db = object_session(user)
        if db is not None and not is_complete(get_profile(db, user.id)):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")


def _user_from_access_token(db: Session, settings: Settings, token: str) -> User:
    try:
        payload = decode_token(settings, token)
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from None
    if payload.get("purpose"):
        # Пропуск на шаг 2 входа подписан тем же ключом, но сессией не является.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    _check_session_gates(settings, user, payload)
    session_role = payload.get("role")
    if session_role in VALID_ROLES and session_role != user.role:
        # Только на время запроса: committed_value не помечает поле изменённым.
        set_committed_value(user, "role", session_role)
        db.info.setdefault("session_roles", {})[user.id] = session_role
    return user


@event.listens_for(User, "refresh")
@event.listens_for(User, "load")
def _reapply_session_role(target, *_args):
    """commit() в середине запроса перечитывает User из БД — вернуть роль сессии."""
    session = object_session(target)
    role = session.info.get("session_roles", {}).get(target.id) if session is not None else None
    if role and target.role != role:
        set_committed_value(target, "role", role)


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> User:
    # Bearer, if sent, wins and must be valid — do not fall back to cookie
    # (tampered Authorization must stay 401 even when a good cookie exists).
    if creds is not None:
        return _user_from_access_token(db, settings, creds.credentials)
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        return _user_from_access_token(db, settings, cookie)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def require_html_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> User:
    """HTML shells: no session → 302 /login (not a JSON 401)."""
    try:
        return get_current_user(request, db, settings, creds)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            raise HTTPException(
                status_code=status.HTTP_302_FOUND,
                detail="Not authenticated",
                headers={"Location": "/login"},
            ) from exc
        raise


def require_role(*roles: str):
    def _dep(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user

    return _dep
