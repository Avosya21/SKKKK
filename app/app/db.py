from __future__ import annotations

import threading
import time
import traceback
import weakref

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import Pool

from app.app.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


def _sqlite_path(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "", 1)
    return url


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_timeout=15,
)

# Кто сейчас держит соединения пула: id соединения -> (имя потока, стек взятия, id потока, время взятия).
# Слушатели на классе Pool: они видят и боевой движок, и движки тестов.
_checked_out: dict[int, tuple[str, str, int, float, "weakref.ref"]] = {}
_checked_out_lock = threading.Lock()


@event.listens_for(Pool, "checkout")
def _track_checkout(dbapi_conn, _record, _proxy) -> None:
    stack = "".join(traceback.format_stack(limit=14)[:-1])
    thread = threading.current_thread()
    with _checked_out_lock:
        try:
            ref = weakref.ref(dbapi_conn)
        except TypeError:  # драйвер без слабых ссылок: запись живёт до checkin
            ref = lambda: True  # noqa: E731
        _checked_out[id(dbapi_conn)] = (thread.name, stack, threading.get_ident(), time.monotonic(), ref)


@event.listens_for(Pool, "checkin")
def _track_checkin(dbapi_conn, _record) -> None:
    with _checked_out_lock:
        _checked_out.pop(id(dbapi_conn), None)


def thread_held_connections() -> list[str]:
    """Стеки взятия соединений, которые сейчас держит ТЕКУЩИЙ поток (пусто — ничего не держит)."""
    me = threading.get_ident()
    with _checked_out_lock:
        # запись о соединении, которое уже уничтожено (пул выброшен без checkin), не считается удерживаемой
        return [stack for _name, stack, ident, _t, ref in _checked_out.values() if ident == me and ref() is not None]


def describe_pool_holders(max_items: int = 4) -> str:
    """Для лога при исчерпании пула: где взяты занятые соединения."""
    with _checked_out_lock:
        items = [v for v in _checked_out.values() if v[4]() is not None]
    head = f"занято соединений: {len(items)}"
    now = time.monotonic()
    parts = [f"--- поток {name}, держит {now - since:.0f} с\n{stack}" for name, stack, _ident, since, _ref in items[:max_items]]
    return head + "\n" + "\n".join(parts)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _connection_record) -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
    # Расширение живёт в рамках соединения, а пул выдаёт их по кругу, поэтому
    # загружать его нужно на каждом новом соединении, а не один раз при старте.
    from app.app.services.vector_index import load_sqlite_vec

    load_sqlite_vec(dbapi_conn)


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Каждый элемент: (таблица, колонка, определение).  ADD COLUMN IF NOT EXISTS
# не поддерживается в SQLite до 3.37, поэтому проверяем PRAGMA table_info.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("artifacts", "transcript_html_url", "VARCHAR(1024)"),
    ("chat_messages", "waiting", "BOOLEAN NOT NULL DEFAULT 0"),
    ("chat_messages", "meta_json", "TEXT"),
    ("meeting_participants", "last_seen_at", "DATETIME"),
    ("project_mcp_tokens", "can_submit", "BOOLEAN NOT NULL DEFAULT 0"),
    ("project_mcp_tokens", "scope", "VARCHAR(24) NOT NULL DEFAULT 'read'"),
    ("project_mcp_tokens", "agent_name", "VARCHAR(64)"),
    ("project_mcp_tokens", "rate_limit", "INTEGER"),
    ("agent_exchange_log", "client_request_id", "VARCHAR(64)"),
    ("artifacts", "recorded_at", "DATETIME"),
    ("artifacts", "recorded_at_source", "VARCHAR(16)"),
    ("chunks", "start_sec", "FLOAT"),
    ("chunks", "end_sec", "FLOAT"),
    ("links", "model_review", "TEXT"),
]


def apply_schema_migrations() -> None:
    """Добавить отсутствующие колонки без Alembic.

    Безопасно вызывать при каждом старте — уже существующие колонки
    пропускаются.
    """
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.connect() as conn:
        for table, column, definition in _MIGRATIONS:
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            if not rows:  # таблицы ещё нет — её целиком создаст create_all с новой колонкой
                continue
            existing = {row[1] for row in rows}
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                conn.commit()
        # Права ключей: прежний флаг can_submit=1 означал «вопросы и вердикты». Строка безопасна при каждом старте:
        # после миграции scope и can_submit меняются только вместе, состояния «can_submit=1, scope=read» больше не возникает.
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(project_mcp_tokens)").fetchall()}
        if {"scope", "can_submit"} <= cols:
            conn.exec_driver_sql("UPDATE project_mcp_tokens SET scope='questions_verdicts' WHERE can_submit=1 AND scope='read'")
            conn.commit()


def get_engine():
    """Читать движок в момент вызова: тесты подменяют модульный `engine`."""
    return engine


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
