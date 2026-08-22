from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.db import get_engine
from app.app.models.artifact import Artifact
from app.app.models.entity import Chunk
from app.app.services.chunking import chunk_plain_text, chunk_transcript
from app.app.services.ollama_client import embed_texts
from app.app.services.vector_index import delete_vectors_for_chunks, init_vector_store, upsert_vectors

logger = logging.getLogger("skkkk.index")


def index_artifact(db: Session, settings: Settings, artifact: Artifact, text: str) -> int:
    init_vector_store(get_engine())

    if artifact.file_type == "audio":
        chunks = chunk_transcript(text)
    else:
        chunks = chunk_plain_text(text)

    # Эмбеддинги считаются до первой записи: иначе блокировка записи в SQLite
    # удерживается всё время сетевого вызова Ollama.
    texts = [c.text for c in chunks]
    vectors = embed_texts(settings, texts) if texts else []

    old_ids = list(db.scalars(select(Chunk.id).where(Chunk.artifact_id == artifact.id)).all())
    if old_ids:
        delete_vectors_for_chunks(db, old_ids)
    db.execute(delete(Chunk).where(Chunk.artifact_id == artifact.id))

    stored: list[Chunk] = []
    for c, vec in zip(chunks, vectors, strict=True):
        row = Chunk(
            artifact_id=artifact.id,
            project_id=artifact.project_id,
            chunk_index=c.index,
            text=c.text,
            speaker=c.speaker,
            token_estimate=c.token_estimate,
        )
        db.add(row)
        stored.append(row)
    db.flush()
    upsert_vectors(db, [(row.id, vec) for row, vec in zip(stored, vectors, strict=True)])
    return len(stored)
