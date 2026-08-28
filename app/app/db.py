from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

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
)


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
            existing = {row[1] for row in rows}
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
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
