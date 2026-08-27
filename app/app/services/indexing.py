from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.db import get_engine
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.entity import Chunk
from app.app.models.project import Project
from app.app.services.chunking import chunk_plain_text, chunk_transcript
from app.app.services.ollama_client import embed_texts
from app.app.services.storage import artifact_knowledge_text
from app.app.services.vector_index import delete_vectors_for_chunks, init_vector_store, upsert_vectors

logger = logging.getLogger("skkkk.index")


def index_artifact(db: Session, settings: Settings, artifact: Artifact, text: str) -> int:
    init_vector_store(get_engine())

    body = (text or "").strip()
    if artifact.file_type == "audio":
        canon = artifact_knowledge_text(settings, artifact)
        if canon:
            body = canon
        chunks = chunk_transcript(body)
    else:
        chunks = chunk_plain_text(body)

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
    speakers = sorted({row.speaker for row in stored if row.speaker})
    log_event(
        logger,
        "index.stored",
        artifact=artifact.artifact_id,
        project_id=artifact.project_id,
        chunks=len(stored),
        with_speaker=sum(1 for row in stored if row.speaker),
        speakers=",".join(speakers[:12]),
        chars=len(body),
        model=settings.ollama_embed_model,
    )
    return len(stored)


def audit_project_index(db: Session, settings: Settings, project_id: int) -> dict:
    """Сверка чанков sqlite-vec с lab ``_final.txt`` внутри одного проекта."""
    from sqlalchemy import text as sql_text

    init_vector_store(get_engine())
    project = db.get(Project, project_id)
    artifacts = db.scalars(
        select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.id)
    ).all()

    conn = db.connection()
    from app.app.services.vector_index import _prepare

    _prepare(conn)
    rows = conn.execute(
        sql_text(
            """
            SELECT c.id AS chunk_pk, c.artifact_id AS artifact_pk, c.speaker,
                   v.chunk_id AS vector_id
            FROM chunks c
            LEFT JOIN chunk_vectors v ON v.chunk_id = c.id
            WHERE c.project_id = :pid
            """
        ),
        {"pid": project_id},
    ).mappings().all()

    by_art: dict[int, dict[str, int]] = {}
    missing_vectors = 0
    for row in rows:
        stats = by_art.setdefault(int(row["artifact_pk"]), {"chunks": 0, "vectors": 0})
        stats["chunks"] += 1
        if row["vector_id"] is None:
            missing_vectors += 1
        else:
            stats["vectors"] += 1

    items: list[dict] = []
    for art in artifacts:
        stats = by_art.get(art.id, {"chunks": 0, "vectors": 0})
        canon = artifact_knowledge_text(settings, art) if art.file_type == "audio" else (art.transcription_text or "")
        chunk_rows = db.scalars(
            select(Chunk).where(Chunk.artifact_id == art.id).order_by(Chunk.chunk_index)
        ).all()
        speakers = sorted({c.speaker for c in chunk_rows if c.speaker})
        aligned = True
        if art.file_type == "audio" and canon and chunk_rows:
            aligned = any((c.text or "")[:40] in canon for c in chunk_rows[:5])
        items.append(
            {
                "artifact_id": art.artifact_id,
                "filename": art.original_filename,
                "status": art.status,
                "file_type": art.file_type,
                "chunks": stats["chunks"],
                "vectors": stats["vectors"],
                "speakers": speakers,
                "canon_chars": len(canon or ""),
                "aligned_with_final": aligned,
            }
        )

    ready = {"indexed", "graph_seeded", "done"}
    problems: list[str] = []
    if missing_vectors:
        problems.append(f"нет вектора у {missing_vectors} чанков")
    known_pks = {art.id for art in artifacts}
    orphan_pks = [pk for pk in by_art if pk not in known_pks]
    if orphan_pks:
        problems.append(f"чанки без артефакта проекта: pk={orphan_pks[:8]}")
    for item in items:
        if item["chunks"] != item["vectors"]:
            problems.append(f"{item['artifact_id']}: chunks={item['chunks']} vectors={item['vectors']}")
        if (
            item["status"] in ready
            and item["file_type"] == "audio"
            and item["canon_chars"]
            and item["chunks"] == 0
        ):
            problems.append(f"{item['artifact_id']}: есть _final.txt, чанков нет")
        if (
            item["status"] in ready
            and item["file_type"] == "audio"
            and item["chunks"]
            and not item["speakers"]
        ):
            problems.append(f"{item['artifact_id']}: чанки без speaker")
        if item["chunks"] and not item["aligned_with_final"]:
            problems.append(f"{item['artifact_id']}: чанки не из lab _final.txt")

    return {
        "project_id": project_id,
        "project_name": project.name if project else "",
        "artifacts": len(artifacts),
        "chunks": sum(i["chunks"] for i in items),
        "vectors": sum(i["vectors"] for i in items),
        "missing_vectors": missing_vectors,
        "ok": not problems,
        "problems": problems,
        "items": items,
    }
