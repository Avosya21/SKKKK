"""Хранилище индекса проекта: SQLite + sqlite-vec + FTS5 в data/project_map.db (отдельно от app.db)."""

from __future__ import annotations

import os
import re
import sqlite3
import struct
import time
from contextlib import contextmanager
from pathlib import Path

from app.app.services.project_index.config import DB_PATH, LOCK_PATH, SCHEMA_VERSION

EMBED_DIM = 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, sha TEXT NOT NULL, size INTEGER, mtime REAL, kind TEXT, indexed_at REAL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL, kind TEXT NOT NULL, title TEXT, symbol TEXT,
    line_start INTEGER, line_end INTEGER, text TEXT NOT NULL, embed_sha TEXT, meta TEXT
);
CREATE INDEX IF NOT EXISTS ix_chunks_path ON chunks(path);
CREATE INDEX IF NOT EXISTS ix_chunks_symbol ON chunks(symbol);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title, symbol, path, idents, text, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS embed_cache (sha TEXT PRIMARY KEY, vec BLOB NOT NULL);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, qualname TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
    line INTEGER, end_line INTEGER, signature TEXT, decorators TEXT, doc TEXT, lang TEXT
);
CREATE INDEX IF NOT EXISTS ix_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS ix_symbols_path ON symbols(path);
CREATE TABLE IF NOT EXISTS calls (
    path TEXT NOT NULL, caller TEXT NOT NULL, callee TEXT NOT NULL, qualifier TEXT, line INTEGER, lang TEXT
);
CREATE INDEX IF NOT EXISTS ix_calls_callee ON calls(callee);
CREATE INDEX IF NOT EXISTS ix_calls_path ON calls(path);
CREATE TABLE IF NOT EXISTS imports (path TEXT NOT NULL, module TEXT NOT NULL, names TEXT, line INTEGER);
CREATE INDEX IF NOT EXISTS ix_imports_module ON imports(module);
CREATE INDEX IF NOT EXISTS ix_imports_path ON imports(path);

CREATE TABLE IF NOT EXISTS routes (
    method TEXT NOT NULL, url TEXT NOT NULL, handler TEXT NOT NULL, path TEXT NOT NULL, line INTEGER,
    deps TEXT, guards TEXT, doc TEXT
);
CREATE INDEX IF NOT EXISTS ix_routes_url ON routes(url);
CREATE TABLE IF NOT EXISTS db_columns (tbl TEXT NOT NULL, col TEXT NOT NULL, ctype TEXT, source TEXT, path TEXT, line INTEGER);
CREATE INDEX IF NOT EXISTS ix_dbcol_tbl ON db_columns(tbl);
CREATE TABLE IF NOT EXISTS table_access (tbl TEXT NOT NULL, path TEXT NOT NULL, symbol TEXT, line INTEGER, writes INTEGER);
CREATE INDEX IF NOT EXISTS ix_tacc_tbl ON table_access(tbl);
CREATE TABLE IF NOT EXISTS env_vars (name TEXT NOT NULL, default_value TEXT, path TEXT, line INTEGER, how TEXT, documented INTEGER);
CREATE INDEX IF NOT EXISTS ix_env_name ON env_vars(name);
CREATE TABLE IF NOT EXISTS ui_links (
    element_id TEXT NOT NULL, tag TEXT, label TEXT, path TEXT, line INTEGER,
    js_path TEXT, js_line INTEGER, js_symbol TEXT, endpoints TEXT
);
CREATE INDEX IF NOT EXISTS ix_ui_id ON ui_links(element_id);
CREATE TABLE IF NOT EXISTS test_cover (test_path TEXT NOT NULL, test_name TEXT, line INTEGER, module TEXT, symbol TEXT);
CREATE INDEX IF NOT EXISTS ix_tc_module ON test_cover(module);
CREATE INDEX IF NOT EXISTS ix_tc_symbol ON test_cover(symbol);
CREATE TABLE IF NOT EXISTS invariants (text TEXT NOT NULL, source TEXT, line INTEGER, guard TEXT);
CREATE TABLE IF NOT EXISTS ui_text (text TEXT NOT NULL, path TEXT, line INTEGER);
CREATE INDEX IF NOT EXISTS ix_uitext ON ui_text(text);
CREATE TABLE IF NOT EXISTS history (hash TEXT PRIMARY KEY, date TEXT, author TEXT, subject TEXT, files TEXT);
"""

# таблицы, которые целиком пересчитываются при любом запуске (дёшево, без эмбеддингов)
DERIVED_TABLES = ["routes", "db_columns", "table_access", "env_vars", "ui_links", "test_cover", "invariants", "ui_text", "history"]
# таблицы, привязанные к файлу: при переиндексации файла его строки удаляются
PER_FILE_TABLES = {"chunks": "path", "symbols": "path", "calls": "path", "imports": "path"}


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def load_vec_ext(conn: sqlite3.Connection) -> None:
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def connect(path: Path | None = None, *, create: bool = False) -> sqlite3.Connection:
    """Открыть индекс. Схема несовместимой версии пересоздаётся (индекс восстановим из кода)."""
    path = path or DB_PATH
    if not path.is_file() and not create:
        raise FileNotFoundError(f"нет индекса {path}; запустите scripts/index_project_map.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    load_vec_ext(conn)
    _register_functions(conn)
    if _needs_rebuild(conn):
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(path) + suffix)
            except OSError:
                pass
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        load_vec_ext(conn)
        _register_functions(conn)
    conn.executescript(SCHEMA)
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{EMBED_DIM}])")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def _register_functions(conn: sqlite3.Connection) -> None:
    # LIKE в SQLite не различает регистр только у латиницы; для русского нужна своя функция
    conn.create_function("PYLOWER", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True)


def _needs_rebuild(conn: sqlite3.Connection) -> bool:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables:
        return False
    if "meta" not in tables:
        return True  # старый индекс версии 1 (map_chunks / map_vectors)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return not row or row[0] != str(SCHEMA_VERSION)


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def identifiers_text(text: str) -> str:
    """Дополнительное поле для FTS: идентификаторы, разбитые на части (camelCase, snake_case)."""
    seen: dict[str, None] = {}
    for word in _WORD.findall(text):
        seen[word] = None
        parts = [p for p in re.split(r"_+", _CAMEL.sub("_", word)) if len(p) > 2]
        if len(parts) > 1:
            for part in parts:
                seen[part.lower()] = None
    return " ".join(list(seen)[:400])


@contextmanager
def index_lock(timeout: float = 5.0):
    """Один индексатор одновременно. Устаревшая блокировка (процесс умер) снимается сама."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    while True:
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            if _lock_is_stale():
                try:
                    os.remove(LOCK_PATH)
                except OSError:
                    pass
                continue
            if time.time() - started > timeout:
                raise RuntimeError("индексация уже идёт (см. data/logs/index.log)")
            time.sleep(0.5)
    try:
        yield
    finally:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass


def _lock_is_stale() -> bool:
    try:
        pid = int(LOCK_PATH.read_text().strip() or 0)
    except (OSError, ValueError):
        return True
    if pid <= 0:
        return True
    if os.name == "nt":
        import subprocess

        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) not in out
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False
