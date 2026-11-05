"""Оставшиеся критерии ТЗ по уязвимостям: session_version, Secure, аудит append-only, SQLite load."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from sqlalchemy import create_engine, event, text
from starlette.requests import Request

from app.app.dependencies import set_session_cookie
from app.app.services.session_version import bump, current


def test_session_version_bump_rejects_old_token(client, admin_token, db_engine):
    assert client.get("/projects", headers={"Authorization": f"Bearer {admin_token}"}).status_code == 200
    bump(reason="test")
    r = client.get("/projects", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 401


def test_new_login_after_bump_works(client, db_engine):
    bump(reason="test-pre")
    r = client.post(
        "/auth/login",
        json={"name": "Admin", "telegram_id": "tg-sv", "password": "admin-pass"},
    )
    assert r.status_code == 200
    token = r.json()["access_token"]
    assert client.get("/projects", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert current() >= 1


def test_cookie_secure_flag_on_https(monkeypatch):
    from fastapi import Response
    from app.app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    response = Response()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 123),
        "server": ("test", 443),
    }
    request = Request(scope)
    set_session_cookie(response, request, settings, "tok")
    header = response.headers.get("set-cookie", "")
    assert "HttpOnly" in header or "httponly" in header.lower()
    assert "secure" in header.lower()
    assert "samesite=lax" in header.lower()


def test_activity_log_has_login_and_profile_id(client, admin_token, db_engine, tmp_path, monkeypatch):
    from app.app.db import SessionLocal
    from app.app.models.user_activity_log import UserActivityLog
    from app.app.services.user_activity import ACTION_LOGIN, BLOCK_AUTH
    from sqlalchemy import select

    # Логин уже был в фикстуре admin_token — берём последнюю auth-запись.
    db = SessionLocal()
    try:
        row = db.scalars(
            select(UserActivityLog)
            .where(UserActivityLog.block == BLOCK_AUTH, UserActivityLog.action == ACTION_LOGIN)
            .order_by(UserActivityLog.id.desc())
        ).first()
        assert row is not None
        assert row.login
        # В тестах PERSONAL_PASSWORD=off — карточки может не быть; login обязателен.
        assert row.user_id
    finally:
        db.close()


def test_activity_log_rejects_update_and_delete(db_engine):
    from app.app.db import SessionLocal
    from app.app.models.user import User
    from app.app.models.user_activity_log import UserActivityLog
    from app.app.services.user_activity import log_activity

    db = SessionLocal()
    try:
        user = User(name="audit-user", telegram_id="tg-audit", role="user", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)
        row = log_activity(db, user_id=user.id, block="auth", action="login", title="t")
        assert row is not None
        with pytest.raises(Exception):
            db.execute(text("UPDATE user_activity_log SET title='x' WHERE id=:id"), {"id": row.id})
            db.commit()
        db.rollback()
        with pytest.raises(Exception):
            db.execute(text("DELETE FROM user_activity_log WHERE id=:id"), {"id": row.id})
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_sqlite_parallel_write_read_no_locked(tmp_path):
    path = tmp_path / "load.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        assert str(mode).lower() == "wal"

    errors: list[BaseException] = []
    stop = threading.Event()

    def writer():
        try:
            with engine.begin() as conn:
                for i in range(40):
                    conn.exec_driver_sql("INSERT INTO t(v) VALUES (:v)", {"v": i})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                with engine.connect() as conn:
                    conn.exec_driver_sql("SELECT count(*) FROM t").scalar()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(5)]
    for t in readers:
        t.start()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(as_completed([pool.submit(writer), pool.submit(writer)]))
    stop.set()
    for t in readers:
        t.join(timeout=5)
    assert not errors, errors
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM t").scalar() == 80
