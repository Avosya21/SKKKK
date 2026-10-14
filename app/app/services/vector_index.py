"""Хранилище эмбеддингов в SQLite через sqlite-vec.

Запись идёт через соединение сессии SQLAlchemy. Отдельное соединение здесь
недопустимо: SQLite допускает только одного писателя, а сессия в этот момент
держит открытую транзакцию — второй писатель гарантированно получал
`database is locked`.

``chunk_vectors`` — vec0, kNN для чата/поиска.
``entity_vectors`` — обычная таблица BLOB по ``entity_id`` (не WeSpeaker, не kNN).
Смена канона ``entity_text`` без смены id делает кэш лживым: тогда DROP/пересоздать таблицу.
"""

from __future__ import annotations

import logging
import struct
from typing import Any

import sqlite_vec
from sqlalchemy import bindparam, text
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
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS entity_vectors (
                    entity_id INTEGER PRIMARY KEY,
                    embedding BLOB NOT NULL
                )
                """
            )
        )


def serialize_float32(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def deserialize_float32(blob: bytes) -> list[float]:
    expected = EMBED_DIM * 4
    if not blob or len(blob) != expected:
        return []
    return list(struct.unpack(f"{EMBED_DIM}f", blob))


_LOAD_BATCH = 400


def upsert_entity_vectors(db: Session, pairs: list[tuple[int, list[float]]]) -> None:
    if not pairs:
        return
    conn = _prepare(db.connection())
    for entity_id, vec in pairs:
        conn.execute(text("DELETE FROM entity_vectors WHERE entity_id = :id"), {"id": entity_id})
        conn.execute(
            text("INSERT INTO entity_vectors(entity_id, embedding) VALUES (:id, :emb)"),
            {"id": entity_id, "emb": serialize_float32(vec)},
        )


def delete_vectors_for_entities(db: Session, entity_ids: list[int]) -> None:
    if not entity_ids:
        return
    conn = _prepare(db.connection())
    for eid in entity_ids:
        conn.execute(text("DELETE FROM entity_vectors WHERE entity_id = :id"), {"id": eid})


def load_entity_vectors(db: Session, entity_ids: list[int]) -> dict[int, list[float]]:
    if not entity_ids:
        return {}
    conn = _prepare(db.connection())
    sql = text("SELECT entity_id, embedding FROM entity_vectors WHERE entity_id IN :ids").bindparams(
        bindparam("ids", expanding=True)
    )
    out: dict[int, list[float]] = {}
    unique = list(dict.fromkeys(entity_ids))
    for start in range(0, len(unique), _LOAD_BATCH):
        batch = unique[start : start + _LOAD_BATCH]
        rows = conn.execute(sql, {"ids": batch}).mappings().all()
        for row in rows:
            vec = deserialize_float32(row["embedding"])
            if len(vec) != EMBED_DIM:
                continue
            out[int(row["entity_id"])] = vec
    return out


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
    from app.app.services.chunk_fts import delete_chunk_fts

    delete_chunk_fts(db, chunk_ids)  # полнотекстовый индекс фрагментов живёт и умирает вместе с векторами


def chunk_scores(db: Session, query_vec: list[float], chunk_ids: list[int]) -> dict[int, float]:
    """Косинусная близость вопроса к заданным фрагментам (для находок полнотекстового поиска)."""
    if not chunk_ids:
        return {}
    conn = _prepare(db.connection())
    marks = ",".join(str(int(c)) for c in chunk_ids)
    rows = conn.execute(
        text(f"SELECT chunk_id, vec_distance_cosine(embedding, :q) AS d FROM chunk_vectors WHERE chunk_id IN ({marks})"),
        {"q": serialize_float32(query_vec)},
    ).all()
    return {int(cid): 1.0 - float(d) for cid, d in rows}


def search_vectors(
    db: Session,
    query_vec: list[float],
    *,
    project_id: int,
    limit: int = 10,
    artifact_id: int | None = None,
) -> list[dict[str, Any]]:
    """Ближайшие к вопросу фрагменты проекта; ``artifact_id`` — только из одного материала (созвон, о котором вопрос)."""
    only_artifact = "AND c.artifact_id = :aid" if artifact_id is not None else ""
    sql = text(
        f"""
        SELECT c.id, c.artifact_id, c.chunk_index, c.text, c.speaker, c.start_sec, c.end_sec, c.unit_first, c.unit_last,
               vec_distance_cosine(v.embedding, :q) AS distance
        FROM chunk_vectors v
        JOIN chunks c ON c.id = v.chunk_id
        WHERE c.project_id = :pid {only_artifact}
        ORDER BY distance ASC
        LIMIT :lim
        """
    )
    conn = _prepare(db.connection())
    rows = conn.execute(
        sql,
        {"q": serialize_float32(query_vec), "pid": project_id, "lim": limit, "aid": artifact_id},
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
                "start_sec": row["start_sec"],
                "end_sec": row["end_sec"],
                "unit_first": row["unit_first"],
                "unit_last": row["unit_last"],
                "score": 1.0 - float(row["distance"]),
            }
        )
    return out
