"""Отдельная база личных паролей `data/credentials.db`: только хеши, отдельно от `app.db`.

Своя декларативная база и свой движок: таблица не попадает в `app.db`, MCP, экспорт и аудиты.
Ключ — логин из `Login_up.txt` без регистра (`login_allowlist.login_key`), а не `users.id`:
слияние дублей пользователей меняет id, логин остаётся.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, DateTime, Engine, Integer, String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.app.config import get_settings

# Тесты подменяют путь на временный файл (conftest), чтобы не трогать боевую базу.
PATH_OVERRIDE: Path | None = None


class CredentialsBase(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PersonalPassword(CredentialsBase):
    __tablename__ = "personal_passwords"

    login_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    # Пусто — пароля нет (не создан или сброшен админом).
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # +1 при каждой смене и сбросе; сессии со старой версией перестают действовать.
    pwd_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reset_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


_engines: dict[Path, tuple[Engine, sessionmaker]] = {}
_lock = threading.Lock()


def db_path() -> Path:
    return PATH_OVERRIDE or get_settings().credentials_db_file


def _make_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    CredentialsBase.metadata.create_all(bind=engine)
    return engine


def _factory() -> sessionmaker:
    path = db_path()
    with _lock:
        if path not in _engines:
            engine = _make_engine(path)
            _engines[path] = (engine, sessionmaker(bind=engine, autoflush=False, autocommit=False))
        return _engines[path][1]


def credentials_session() -> Session:
    """Сессия базы личных паролей; таблица создаётся при первом обращении."""
    return _factory()()


def dispose_all() -> None:
    """Закрыть все движки (тесты, чтобы Windows отпустил файл)."""
    with _lock:
        for engine, _ in _engines.values():
            engine.dispose()
        _engines.clear()
