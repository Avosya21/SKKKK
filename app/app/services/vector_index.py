"""Хранилище эмбеддингов в SQLite через sqlite-vec.

Запись идёт через соединение сессии SQLAlchemy. Отдельное соединение здесь
недопустимо: SQLite допускает только одного писателя, а сессия в этот момент
держит открытую транзакцию — второй писатель гарантированно получал
`database is locked`.
"""

from __future__ import annotations

import logging
import struct
from typing import Any

import sqlite_vec
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.app.services.ollama_client import EMBED_DIM

logger = logging.getLogger("skkkk.vector")


def load_sqlite_vec(dbapi_conn) -> bool:
    """Подключить расширение к конкретному соединению SQLite."""
    try:
        dbapi_conn.enable_load_extension(True)
        sqlite_vec.load(dbapi_conn)
        return True
    except Exception as exc:
        logger.warning("sqlite-vec not loaded: %s", exc)
        return False
    finally:
        try:
            dbapi_conn.enable_load_extension(False)
        except Exception:
            pass


def _prepare(conn: Connection) -> Connection:
    load_sqlite_vec(conn.connection.dbapi_connection)
    return conn


def init_vector_store(engine: Engine) -> None:
    with engine.begin() as conn:
        _prepare(conn)
        conn.execute(
            text(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0(
                    chunk_id INTEGER PRIMARY KEY,
                    embedding float[{EMBED_DIM}]
                )
                """
            )
        )


def serialize_float32(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def upsert_vectors(db: Session, pairs: list[tuple[int, list[float]]]) -> None:
    if not pairs:
        return
    conn = _prepare(db.connection())
    for chunk_id, vec in pairs:
        conn.execute(text("DELETE FROM chunk_vectors WHERE chunk_id = :id"), {"id": chunk_id})
        conn.execute(
            text("INSERT INTO chunk_vectors(chunk_id, embedding) VALUES (:id, :emb)"),
            {"id": chunk_id, "emb": serialize_float32(vec)},
        )


def delete_vectors_for_chunks(db: Session, chunk_ids: list[int]) -> None:
    if not chunk_ids:
        return
    conn = _prepare(db.connection())
    for cid in chunk_ids:
        conn.execute(text("DELETE FROM chunk_vectors WHERE chunk_id = :id"), {"id": cid})


def search_vectors(
    db: Session,
    query_vec: list[float],
    *,
    project_id: int,
    limit: int = 10,
) -> list[dict[str, Any]]:
    sql = text(
        """
        SELECT c.id, c.artifact_id, c.chunk_index, c.text, c.speaker,
               vec_distance_cosine(v.embedding, :q) AS distance
        FROM chunk_vectors v
        JOIN chunks c ON c.id = v.chunk_id
        WHERE c.project_id = :pid
        ORDER BY distance ASC
        LIMIT :lim
        """
    )
    conn = _prepare(db.connection())
    rows = conn.execute(
        sql,
        {"q": serialize_float32(query_vec), "pid": project_id, "lim": limit},
    ).mappings().all()

    out = []
    for row in rows:
        out.append(
            {
                "chunk_id": row["id"],
                "artifact_id": row["artifact_id"],
                "chunk_index": row["chunk_index"],
                "text": row["text"],
                "speaker": row["speaker"],
                "score": 1.0 - float(row["distance"]),
            }
        )
    return out
