from __future__ import annotations

import threading
import time
import traceback
import weakref
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import Pool

from app.app.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


def _sqlite_path(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "", 1)
    return url


def _ensure_sqlite_parent(url: str) -> None:
    """Создать каталог файла SQLite: в свежем клоне data/ ещё нет, а СУБД его не создаёт."""
    if not url.startswith("sqlite:///"):
        return
    raw = _sqlite_path(url)
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        return
    Path(raw).parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_parent(settings.database_url)

_sqlite = settings.database_url.startswith("sqlite")
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False, **({"timeout": 30} if _sqlite else {})},
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
    # Кадры SQLAlchemy занимали весь стек, и место в коде портала в предупреждение не попадало (worker.log 2026-10-08).
    frames = [f for f in traceback.format_stack(limit=60)[:-1] if "site-packages" not in f and 'File "<string>"' not in f]
    stack = "".join(frames[-14:])
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


# --- транзакция «только чтение» перед долгим вызовом модели --------------------------------------------------------------
# Корень предупреждений «LLM-вызов при открытой транзакции»: после commit SQLAlchemy считает объекты устаревшими, и первое же
# чтение поля (artifact.file_type, artifact.artifact_id) молча открывает НОВУЮ транзакцию и берёт соединение из пула. Если
# следом идёт вызов модели, соединение занято всё время ответа. Расставлять db.commit() перед каждым таким местом ненадёжно:
# поле читает и сама функция, которая зовёт модель. Поэтому поток, который это разрешил (воркер), перед вызовом модели сам
# закрывает транзакции, в которых ничего не менялось. Транзакцию с изменениями не трогаем — о ней по-прежнему предупреждение.
_idle_release_threads: set[int] = set()
_thread_sessions: dict[int, "weakref.WeakSet[Session]"] = {}


def allow_idle_release_in_this_thread() -> None:
    """Разрешить этому потоку закрывать перед вызовом модели свои транзакции без изменений. Только для потока, который
    один владеет своими сессиями (главный цикл воркера): в портале сессия запроса может жить в другом потоке пула."""
    _idle_release_threads.add(threading.get_ident())


@event.listens_for(Session, "after_begin")
def _track_session_begin(session, _transaction, _connection) -> None:
    ident = threading.get_ident()
    if ident in _idle_release_threads:
        _thread_sessions.setdefault(ident, weakref.WeakSet()).add(session)


@event.listens_for(Session, "after_flush")
def _mark_session_wrote(session, _context) -> None:
    session.info["pa_wrote"] = True  # в транзакции есть записанные, но не зафиксированные изменения


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def _clear_session_wrote(session) -> None:
    session.info.pop("pa_wrote", None)


def release_idle_read_transactions() -> int:
    """Закрыть транзакции текущего потока, в которых ничего не менялось (только чтение). Вернуть, сколько закрыто."""
    ident = threading.get_ident()
    if ident not in _idle_release_threads:
        return 0
    released = 0
    for session in list(_thread_sessions.get(ident) or ()):
        if not session.in_transaction() or session.info.get("pa_wrote") or session.new or session.dirty or session.deleted:
            continue
        session.commit()
        released += 1
    return released


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
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
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
    ("jira_items", "fmt", "INTEGER"),
    ("jira_project_links", "issues_total", "INTEGER"),
    ("artifacts", "transcript_html_url", "VARCHAR(1024)"),
    ("subjects", "judged_sig", "VARCHAR(64)"),
    ("chat_messages", "waiting", "BOOLEAN NOT NULL DEFAULT 0"),
    ("chat_messages", "meta_json", "TEXT"),
    ("meeting_participants", "last_seen_at", "DATETIME"),
    ("project_mcp_tokens", "can_submit", "BOOLEAN NOT NULL DEFAULT 0"),
    ("project_mcp_tokens", "scope", "VARCHAR(24) NOT NULL DEFAULT 'read'"),
    ("project_mcp_tokens", "agent_name", "VARCHAR(64)"),
    ("project_mcp_tokens", "rate_limit", "INTEGER"),
    ("project_mcp_tokens", "files_access", "VARCHAR(8) NOT NULL DEFAULT 'none'"),
    ("agent_exchange_log", "client_request_id", "VARCHAR(64)"),
    ("artifacts", "recorded_at", "DATETIME"),
    ("artifacts", "recorded_at_source", "VARCHAR(16)"),
    ("chunks", "start_sec", "FLOAT"),
    ("chunks", "end_sec", "FLOAT"),
    ("chunks", "unit_first", "VARCHAR(24)"),
    ("chunks", "unit_last", "VARCHAR(24)"),
    ("links", "model_review", "TEXT"),
    ("speaker_profiles", "user_id", "INTEGER REFERENCES users(id)"),
    ("entities", "topic_id", "INTEGER"),
    # Платформа памяти решений (PLATFORM-FOUNDATION-PLAN, ПФ1): человек из реестра и точность времени материала.
    ("speaker_profiles", "person_id", "INTEGER REFERENCES people(id)"),
    ("meeting_participants", "person_id", "INTEGER REFERENCES people(id)"),
    # ЛГ4/Р7 (PEOPLE-FLOW-PLAN): компания, которую гость указал при входе во встречу
    ("meeting_participants", "company_name", "VARCHAR(200)"),
    ("people", "company_unconfirmed", "BOOLEAN NOT NULL DEFAULT 0"),
    ("people", "needs_review", "BOOLEAN NOT NULL DEFAULT 0"),
    ("artifacts", "recorded_at_precision", "VARCHAR(8)"),
    ("user_activity_log", "login", "VARCHAR(128)"),
    ("user_activity_log", "profile_id", "INTEGER"),
]

_AUDIT_APPEND_ONLY_SQL = (
    "CREATE TRIGGER IF NOT EXISTS user_activity_log_no_update "
    "BEFORE UPDATE ON user_activity_log "
    "BEGIN SELECT RAISE(ABORT, 'user_activity_log is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS user_activity_log_no_delete "
    "BEFORE DELETE ON user_activity_log "
    "BEGIN SELECT RAISE(ABORT, 'user_activity_log is append-only'); END",
)


def ensure_audit_append_only(conn) -> None:
    """Триггеры: запрет UPDATE/DELETE в журнале аудита (после backfill колонок)."""
    rows = conn.exec_driver_sql("PRAGMA table_info(user_activity_log)").fetchall()
    if not rows:
        return
    for stmt in _AUDIT_APPEND_ONLY_SQL:
        conn.exec_driver_sql(stmt)


def ensure_user_activity_log_no_fk(conn) -> None:
    """Убрать унаследованные FK журнала на users/projects.

    Модель и DECISIONS — без FK: purge проекта и слияние учёток оставляют строки аудита.
    Старая схема с FK ломала DELETE projects (FOREIGN KEY constraint failed), а
    PRAGMA foreign_keys=OFF внутри транзакции SQLite — no-op.
    """
    rows = conn.exec_driver_sql("PRAGMA table_info(user_activity_log)").fetchall()
    if not rows:
        return
    fks = conn.exec_driver_sql("PRAGMA foreign_key_list(user_activity_log)").fetchall()
    if not fks:
        return
    cols = {row[1] for row in rows}
    # Явный список — порядок как в модели; лишние будущие колонки не потеряем через SELECT *.
    wanted = [
        "id",
        "user_id",
        "login",
        "profile_id",
        "project_id",
        "block",
        "action",
        "title",
        "content",
        "target_kind",
        "target_id",
        "meta_json",
        "created_at",
    ]
    select_cols = ", ".join(c for c in wanted if c in cols)
    # DROP дочерней таблицы с FK на projects/users — при FK=ON допустим; pragma не нужен.
    conn.exec_driver_sql(
        "CREATE TABLE user_activity_log__nofk ("
        "id INTEGER NOT NULL PRIMARY KEY,"
        "user_id INTEGER NOT NULL,"
        "login VARCHAR(128),"
        "profile_id INTEGER,"
        "project_id INTEGER,"
        "block VARCHAR(32) NOT NULL,"
        "action VARCHAR(48) NOT NULL,"
        "title VARCHAR(255) NOT NULL,"
        "content TEXT,"
        "target_kind VARCHAR(32),"
        "target_id VARCHAR(128),"
        "meta_json TEXT,"
        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL"
        ")"
    )
    conn.exec_driver_sql(
        f"INSERT INTO user_activity_log__nofk ({select_cols}) "
        f"SELECT {select_cols} FROM user_activity_log"
    )
    conn.exec_driver_sql("DROP TABLE user_activity_log")
    conn.exec_driver_sql("ALTER TABLE user_activity_log__nofk RENAME TO user_activity_log")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_user_activity_user_project_id "
        "ON user_activity_log (user_id, project_id, id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_user_activity_project_block "
        "ON user_activity_log (project_id, block)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_user_activity_log_user_id ON user_activity_log (user_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_user_activity_log_login ON user_activity_log (login)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_user_activity_log_profile_id ON user_activity_log (profile_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_user_activity_log_project_id ON user_activity_log (project_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_user_activity_log_block ON user_activity_log (block)"
    )
    # Триггеры висели на старой таблице — ставим заново.
    for stmt in _AUDIT_APPEND_ONLY_SQL:
        conn.exec_driver_sql(stmt)
    conn.commit()


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
        # Точность даты материала (ПФ6): у уже загруженных выводится из источника; новые пишут её сами (recorded_at.set_recorded)
        art_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(artifacts)").fetchall()}
        if {"recorded_at_precision", "recorded_at_source"} <= art_cols:
            conn.exec_driver_sql(
                "UPDATE artifacts SET recorded_at_precision = CASE WHEN recorded_at_source IN "
                "('meeting','media','manual_time','filename_time','messages') THEN 'minute' ELSE 'day' END "
                "WHERE recorded_at_precision IS NULL AND recorded_at_source IS NOT NULL"
            )
            conn.commit()
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(project_mcp_tokens)").fetchall()}
        if {"scope", "can_submit"} <= cols:
            conn.exec_driver_sql("UPDATE project_mcp_tokens SET scope='questions_verdicts' WHERE can_submit=1 AND scope='read'")
            conn.commit()
        # Аудит: до установки триггеров дозаполняем login/profile_id (после — UPDATE запрещён).
        act_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(user_activity_log)").fetchall()}
        has_no_update = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='user_activity_log_no_update'"
        ).fetchone()
        if act_cols and not has_no_update:
            if "login" in act_cols:
                conn.exec_driver_sql(
                    "UPDATE user_activity_log SET login = (SELECT name FROM users WHERE users.id = user_activity_log.user_id) "
                    "WHERE login IS NULL"
                )
            if "profile_id" in act_cols:
                conn.exec_driver_sql(
                    "UPDATE user_activity_log SET profile_id = user_id "
                    "WHERE profile_id IS NULL AND EXISTS (SELECT 1 FROM user_profiles WHERE user_profiles.user_id = user_activity_log.user_id)"
                )
            conn.commit()
        ensure_user_activity_log_no_fk(conn)
        ensure_audit_append_only(conn)
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
