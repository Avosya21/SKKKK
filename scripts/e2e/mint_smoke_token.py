"""Выпустить JWT для живого e2e/smoke без OTP (только локальная машина).

Требует E2E_ALLOW_MINT=1. Печатает токен в stdout (одна строка) — его кладут в SMOKE_BEARER_TOKEN.

    $env:E2E_ALLOW_MINT=1
    $env:SMOKE_BEARER_TOKEN = (venv\\Scripts\\python.exe scripts\\e2e\\mint_smoke_token.py)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main() -> int:
    if os.environ.get("E2E_ALLOW_MINT", "").strip() not in {"1", "true", "yes"}:
        print("FAIL: задайте E2E_ALLOW_MINT=1", file=sys.stderr)
        return 1

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.dependencies import create_access_token
    from app.app.models.user import User
    from app.app.services.login_allowlist import login_key
    from app.app.services.personal_password import current_version
    from app.app.services.session_version import current as current_session_version
    from app.app.services.user_identity import find_users_by_login_name, pick_canonical_user

    login = os.environ.get("SMOKE_LOGIN") or ""
    if not login:
        path = ROOT / "Login_up.txt"
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    login = line
                    break
    if not login:
        print("FAIL: нет SMOKE_LOGIN / Login_up.txt", file=sys.stderr)
        return 1

    role = os.environ.get("SMOKE_ROLE") or "admin"
    settings = get_settings()
    db = SessionLocal()
    try:
        from app.app.services.user_profile import get_profile, is_complete

        found = find_users_by_login_name(db, login)
        user = pick_canonical_user(db, found) if found else db.query(User).filter(User.name == login).first()
        if user is None:
            print(f"FAIL: пользователь {login!r} не найден", file=sys.stderr)
            return 1
        # Карточка должна быть заполнена (personal_password=required), иначе API даст 401.
        if not is_complete(get_profile(db, user.id)):
            alt = None
            for cand in db.query(User).filter(User.role.in_(("admin", "manager"))).all():
                if is_complete(get_profile(db, cand.id)):
                    alt = cand
                    break
            if alt is None:
                print(f"FAIL: у {login!r} нет заполненной карточки и запасного admin/manager", file=sys.stderr)
                return 1
            print(f"WARN: карточка {login!r} неполная — беру {alt.name!r}", file=sys.stderr)
            user = alt
        pv = current_version(user.name)
        token = create_access_token(
            settings,
            user,
            role,
            pv=pv,
            pp=True,
            pf=True,
            sv=current_session_version(),
        )
        print(token)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
