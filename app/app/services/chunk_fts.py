"""Полнотекстовый поиск по фрагментам проекта (SQLite FTS5) — пара к векторному поиску для точных слов и чисел.

Таблица ``chunk_fts`` (rowid = chunks.id) хранит текст фрагмента в «поисковом» виде: разряды чисел склеены
(«240 000» → «240000»), чтобы вопрос «240 000» и «240000» находил одно и то же. Пишется в ``index_artifact``,
чистится вместе с векторами (``vector_index.delete_vectors_for_chunks``); недостающие строки дозаполняются при
первом обращении процесса к базе.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

logger = logging.getLogger("primaryanalitics.fts")

_READY: set[str] = set()
_DIGIT_GROUP = re.compile(r"(?<=\d)[   ](?=\d{3}(?!\d))")
_WORD = re.compile(r"\w+", re.U)
MIN_WORD = 3
STEM_MIN = 4  # слова от 5 букв ищем по основе: без двух последних букв, но не короче 4 («стоит» → «стои*»)
STOPWORDS = frozenset(
    """какой какая какие каким какого каких какую сколько что чем чего это эти этот эта этом для как где когда кто
    все всё всех был была были было будет есть нет или так там тут про при под над без его ее её них нам вам мне
    нас вас они она оно тоже также ещё еще уже очень можно нужно надо какое который которая которые которых чтобы
    если ли либо между через после перед проекте проекта проект документ документе документа скажи подскажи
    покажи найди расскажи the and for with from what how""".split()
)


def fts_normalize(value: str) -> str:
    return _DIGIT_GROUP.sub("", value or "")


def _create(conn: Connection) -> None:
    conn.execute(
        text("CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(body, tokenize='unicode61 remove_diacritics 2')")
    )


def _backfill(conn: Connection) -> int:
    rows = conn.execute(
        text("SELECT id, text FROM chunks WHERE id NOT IN (SELECT rowid FROM chunk_fts)")
    ).all()
    for cid, body in rows:
        conn.execute(text("INSERT INTO chunk_fts(rowid, body) VALUES (:id, :b)"), {"id": cid, "b": fts_normalize(body or "")})
    return len(rows)


def init_chunk_fts(engine: Engine) -> None:
    key = str(engine.url)
    if key in _READY:
        return
    with engine.begin() as conn:
        _create(conn)
        added = _backfill(conn)
    if added:
        logger.info("chunk_fts backfilled %s chunks", added)
    _READY.add(key)


def _table_exists(conn: Connection) -> bool:
    return bool(conn.execute(text("SELECT 1 FROM sqlite_master WHERE name = 'chunk_fts'")).first())


def delete_chunk_fts(db: Session, chunk_ids: list[int]) -> None:
    if not chunk_ids:
        return
    conn = db.connection()
    if not _table_exists(conn):
        return
    for cid in chunk_ids:
        conn.execute(text("DELETE FROM chunk_fts WHERE rowid = :id"), {"id": cid})


def upsert_chunk_fts(db: Session, pairs: list[tuple[int, str]]) -> None:
    if not pairs:
        return
    conn = db.connection()
    _create(conn)
    for cid, body in pairs:
        conn.execute(text("DELETE FROM chunk_fts WHERE rowid = :id"), {"id": cid})
        conn.execute(text("INSERT INTO chunk_fts(rowid, body) VALUES (:id, :b)"), {"id": cid, "b": fts_normalize(body or "")})


def build_match_query(question: str) -> str:
    """Вопрос → запрос FTS5: значимые слова (основа + ``*``) и все числа через OR; пусто — искать нечего."""
    terms: list[str] = []
    for word in _WORD.findall(fts_normalize(question).lower()):
        if word.isdigit():
            term = f'"{word}"'
        elif len(word) < MIN_WORD or word in STOPWORDS:
            continue
        elif len(word) > STEM_MIN:
            term = f'"{word[: max(STEM_MIN, len(word) - 2)]}"*'
        else:
            term = f'"{word}"*'
        if term not in terms:
            terms.append(term)
    return " OR ".join(terms[:24])


def search_chunk_fts(
    db: Session, question: str, *, project_id: int, limit: int = 20, artifact_id: int | None = None
) -> list[dict[str, Any]]:
    query = build_match_query(question)
    if not query:
        return []
    conn = db.connection()
    if not _table_exists(conn):
        return []
    try:
        rows = conn.execute(
            text(
                """
                SELECT c.id, c.artifact_id, c.chunk_index, c.text, c.speaker, c.start_sec, c.end_sec, c.unit_first, c.unit_last,
                       bm25(chunk_fts) AS rank
                FROM chunk_fts f
                JOIN chunks c ON c.id = f.rowid
                WHERE chunk_fts MATCH :q AND c.project_id = :pid AND (:aid IS NULL OR c.artifact_id = :aid)
                ORDER BY rank
                LIMIT :lim
                """
            ),
            {"q": query, "pid": project_id, "lim": limit, "aid": artifact_id},
        ).mappings().all()
    except Exception as exc:  # синтаксис FTS5 на странном вопросе — не валим чат, остаётся вектор
        logger.warning("chunk_fts search failed q=%r: %s", query, exc)
        return []
    return [
        {
            "chunk_id": r["id"],
            "artifact_id": r["artifact_id"],
            "chunk_index": r["chunk_index"],
            "text": r["text"],
            "speaker": r["speaker"],
            "start_sec": r["start_sec"],
            "end_sec": r["end_sec"],
            "unit_first": r["unit_first"],
            "unit_last": r["unit_last"],
            "bm25": float(r["rank"]),
        }
        for r in rows
    ]
