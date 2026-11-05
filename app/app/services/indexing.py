"""Индексация артефакта для поиска по смыслу (фрагменты и векторы в sqlite-vec; переписка — по сообщениям) и сверка индекса проекта (`audit_project_index`)."""

from __future__ import annotations

import logging
import re

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.db import get_engine
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.entity import Chunk
from app.app.models.project import Project
from app.app.services.chunk_fts import upsert_chunk_fts
from app.app.services.chat_export import load_artifact_export, owned_elsewhere
from app.app.services.chunking import chunk_document, chunk_messages, chunk_transcript
from app.app.services.ollama_client import embed_texts
from app.app.services.storage import artifact_knowledge_text, parsed_dir
from app.app.services.vector_index import delete_vectors_for_chunks, init_vector_store, upsert_vectors

logger = logging.getLogger("primaryanalitics.index")
_TABLE_CHUNK_RE = re.compile(r"^(?:\| --- |Строка \d+: )", re.M)


def index_artifact(db: Session, settings: Settings, artifact: Artifact, text: str) -> int:
    init_vector_store(get_engine())

    body = (text or "").strip()
    if artifact.file_type == "audio":
        canon = artifact_knowledge_text(settings, artifact)
        if canon:
            body = canon
        chunks = chunk_transcript(body)
    elif (export := load_artifact_export(settings, artifact)) and export.get("messages"):
        # Переписка: фрагменты по сообщениям с номерами M…; сообщения из более ранней выгрузки той же переписки — не здесь (Я1)
        skip = owned_elsewhere(parsed_dir(settings, artifact.project_id, artifact.source_type), export)
        chunks = chunk_messages(export, skip_ids=skip)
    else:
        chunks = chunk_document(body)  # таблицы — целыми строками с шапкой, проза — абзацами с разделом

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
            start_sec=c.start_sec,
            end_sec=c.end_sec,
            unit_first=c.unit_first,
            unit_last=c.unit_last,
        )
        db.add(row)
        stored.append(row)
    db.flush()
    upsert_vectors(db, [(row.id, vec) for row, vec in zip(stored, vectors, strict=True)])
    upsert_chunk_fts(db, [(row.id, row.text) for row in stored])
    speakers = sorted({row.speaker for row in stored if row.speaker})
    log_event(
        logger,
        "index.stored",
        artifact=artifact.artifact_id,
        project_id=artifact.project_id,
        chunks=len(stored),
        table_chunks=sum(1 for row in stored if _TABLE_CHUNK_RE.search(row.text or "")),
        with_speaker=sum(1 for row in stored if row.speaker),
        speakers=",".join(speakers[:12]),
        chars=len(body),
        model=settings.ollama_embed_model,
    )
    if artifact.file_type == "audio":
        from app.app.services.entities import rebind_call_entities

        rebind_call_entities(db, settings, artifact)
    return len(stored)


def chat_export_siblings(db: Session, settings: Settings, artifact: Artifact) -> list[str]:
    """Другие выгрузки той же переписки (то же название) в проекте — их индекс зависит от этой (правило Я1).
    Возвращает ``artifact_id``; у материала без разметки сообщений — пусто."""
    export = load_artifact_export(settings, artifact)
    title = (export or {}).get("title") or ""
    if not title:
        return []
    from app.app.services.chat_export import load_export

    out: list[str] = []
    for src in {artifact.source_type, "document", "note"}:
        folder = parsed_dir(settings, artifact.project_id, src)
        for path in folder.glob("*_messages.json") if folder.is_dir() else []:
            other = load_export(path)
            if other and other.get("title") == title and other.get("artifact_id") and other["artifact_id"] != artifact.artifact_id:
                out.append(str(other["artifact_id"]))
    return sorted(set(out))


def reindex_artifacts(settings: Settings, artifact_ids: list[str]) -> int:
    """Переиндексировать материалы по ``artifact_id`` в своей сессии БД (фоновая задача после ответа HTTP). Текст —
    тот же, что у материала; модели нет, только эмбеддинги. Возвращает число фрагментов."""
    from app.app.db import SessionLocal

    total = 0
    with SessionLocal() as db:
        for aid in artifact_ids:
            art = db.scalar(select(Artifact).where(Artifact.artifact_id == aid))
            if art is None or art.status != "done":
                continue
            try:
                total += index_artifact(db, settings, art, artifact_knowledge_text(settings, art) or art.transcription_text or "")
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("reindex failed for %s", aid)
    return total


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
        timed = sum(1 for c in chunk_rows if c.start_sec is not None)
        aligned = True
        if art.file_type == "audio" and canon and chunk_rows:
            # Строки чанка — строки канона (окно реплик), поэтому сверяем построчно.
            aligned = any(
                line[:40] in canon for c in chunk_rows[:5] for line in (c.text or "").splitlines() if line.strip()
            )
        items.append(
            {
                "artifact_id": art.artifact_id,
                "filename": art.original_filename,
                "status": art.status,
                "file_type": art.file_type,
                "chunks": stats["chunks"],
                "vectors": stats["vectors"],
                "speakers": speakers,
                "timed_chunks": timed,
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
            and not item["timed_chunks"]  # окна реплик: имена в тексте, speaker только у окна одного говорящего
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
