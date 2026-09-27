"""Поиск сущностей и фрагментов проекта: взвешенные оценки, обогащение и переранжирование результатов, порядок артефактов."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.services.graph import _cosine, entity_text
from app.app.services.vector_index import load_entity_vectors

MATERIAL_ENTITY_TYPES = frozenset({"decision", "task", "fact"})
ENTITY_SEARCH_LIMIT = 12


def search_entities(
    db: Session,
    query_vec: list[float],
    *,
    project_id: int,
    limit: int = ENTITY_SEARCH_LIMIT,
    min_score: float = 0.35,
) -> list[dict[str, Any]]:
    """Ближайшие decision/task/fact проекта по уже посчитанному вектору вопроса."""
    rows = db.execute(
        select(Entity, Artifact)
        .join(Artifact, Artifact.id == Entity.artifact_id)
        .where(
            Artifact.project_id == project_id,
            Entity.is_active.is_(True),
            Entity.entity_type.in_(MATERIAL_ENTITY_TYPES),
        )
        .order_by(Entity.id.asc())
    ).all()
    if not rows:
        return []
    vectors = load_entity_vectors(db, [ent.id for ent, _art in rows])
    ranked: list[tuple[float, Entity, Artifact]] = []
    for ent, art in rows:
        vec = vectors.get(ent.id)
        if not vec:
            continue
        score = _cosine(query_vec, vec)
        if score < min_score:
            continue
        ranked.append((score, ent, art))
    ranked.sort(key=lambda item: (-item[0], -item[1].id))
    out: list[dict[str, Any]] = []
    for score, ent, art in ranked[:limit]:
        out.append(
            {
                "entity_id": ent.id,
                "entity_type": ent.entity_type,
                "value_json": ent.value_json,
                "text": entity_text(ent.value_json),
                "line": format_entity_line(ent.entity_type, ent.value_json, art),
                "score": score,
                "artifact_db_id": art.id,
                "artifact_id": art.artifact_id,
                "filename": art.original_filename,
                "priority_weight": art.priority_weight,
            }
        )
    return out


def weighted_score(semantic_score: float, priority_weight: float) -> float:
    """Семантика × (0.5 + priority_weight): созвон 0.5 → ×1.0, документ 0.8 → ×1.3."""
    return float(semantic_score) * (0.5 + float(priority_weight))


def enrich_and_rerank_hits(
    db: Session,
    hits: list[dict[str, Any]],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if not hits:
        return []
    artifact_ids = {h["artifact_id"] for h in hits}
    artifacts = {
        a.id: a
        for a in db.scalars(select(Artifact).where(Artifact.id.in_(artifact_ids))).all()
    }
    enriched: list[dict[str, Any]] = []
    for hit in hits:
        art = artifacts.get(hit["artifact_id"])
        weight = art.priority_weight if art else 0.5
        row = dict(hit)
        row["priority_weight"] = weight
        row["weighted_score"] = weighted_score(hit["score"], weight)
        if art:
            row["source_type"] = art.source_type
            row["filename"] = art.original_filename
        enriched.append(row)
    enriched.sort(key=lambda row: row["weighted_score"], reverse=True)
    if limit is not None:
        return enriched[:limit]
    return enriched


RRF_K = 60
FTS_TRUSTED_TOP = 5  # первые находки по точным словам проходят и ниже порога смысловой близости


def fuse_chunk_hits(
    db: Session,
    question: str,
    query_vec: list[float],
    vec_hits: list[dict[str, Any]],
    *,
    project_id: int,
    limit: int,
    min_score: float,
    rerank=None,
) -> list[dict[str, Any]]:
    """Смысловые находки ``vec_hits`` + полнотекстовые (FTS5) → Reciprocal Rank Fusion с весом источника.

    ``score`` — косинусная близость (как раньше); порог ``min_score`` не отсекает первые находки по точным словам
    и числам — именно их эмбеддинг ищет хуже всего («сколько стоит этап 3», артикулы, номера).
    ``rerank`` — обогащение весом источника (по умолчанию ``enrich_and_rerank_hits``).
    """
    from app.app.services.chunk_fts import search_chunk_fts
    from app.app.services.vector_index import chunk_scores

    fts_hits = search_chunk_fts(db, question, project_id=project_id, limit=limit * 2)
    fused: dict[int, dict[str, Any]] = {}
    for rank, hit in enumerate(vec_hits):
        fused[hit["chunk_id"]] = {**hit, "rrf": 1.0 / (RRF_K + rank + 1)}
    fts_only: list[int] = []
    for rank, hit in enumerate(fts_hits):
        row = fused.get(hit["chunk_id"])
        if row is None:
            row = {**hit, "rrf": 0.0}
            fused[hit["chunk_id"]] = row
            fts_only.append(hit["chunk_id"])
        row["rrf"] += 1.0 / (RRF_K + rank + 1)
        row["fts_rank"] = rank
    if fts_only:
        try:
            scores = chunk_scores(db, query_vec, fts_only)
        except Exception:  # нет таблицы векторов (тесты, пустая база) — близость неизвестна
            scores = {}
        for cid in fts_only:
            fused[cid]["score"] = scores.get(cid, 0.0)
    rows = (rerank or enrich_and_rerank_hits)(db, list(fused.values()))
    rows = [h for h in rows if h["score"] >= min_score or h.get("fts_rank", FTS_TRUSTED_TOP) < FTS_TRUSTED_TOP]
    rows.sort(key=lambda h: h.get("rrf", 0.0) * (0.5 + float(h.get("priority_weight", 0.5))), reverse=True)
    return rows[:limit]


def _source_title(artifact: Artifact | None) -> str:
    if artifact is None:
        return ""
    kind = "Созвон" if artifact.file_type == "audio" else "Документ"
    return f"{kind} «{artifact.original_filename}»"


def chunk_header(artifact: Artifact | None, hit: dict[str, Any]) -> str:
    """Шапка фрагмента для LLM: откуда, когда записан, какой отрезок. Пример:
    ``[Созвон «Сдача тикета 2.webm» · созвон 21.09.2026 · 05:51–07:34 · artifact_id=art_…]``."""
    from app.app.services.recorded_at import material_date_label
    from app.app.services.transcript_turns import format_timecode

    if artifact is None:
        return ""
    parts = [_source_title(artifact)]
    label = material_date_label(artifact)
    if label:
        parts.append(label)
    start, end = hit.get("start_sec"), hit.get("end_sec")
    if start is not None:
        parts.append(f"{format_timecode(start)}–{format_timecode(end if end is not None else start)}")
    parts.append(f"artifact_id={artifact.artifact_id}")
    return "[" + " · ".join(parts) + "]"


def _entity_body(entity_type: str, value: dict[str, Any]) -> str:
    if entity_type == "fact" and "key" in value:
        return f"{value.get('key')}: {value.get('value')}"
    if entity_type in ("participant", "mention") and value.get("name"):
        extra = value.get("role") or (value.get("relation") if entity_type == "mention" else None)
        return f"{value['name']} ({extra})" if extra else str(value["name"])
    body = str(value.get("text") or value.get("description") or "").strip()
    if entity_type == "open_question" and value.get("options"):
        body = f"{body} (варианты: {'; '.join(str(o) for o in value['options'])})"
    if entity_type == "task":
        extra = []
        if value.get("assignee"):
            extra.append(f"исполнитель: {value['assignee']}")
        if value.get("deadline"):
            extra.append(f"срок: {value['deadline']}")
        if extra:
            body = f"{body} ({'; '.join(extra)})"
    return body or entity_text(json.dumps(value, ensure_ascii=False))


def format_entity_line(entity_type: str, value_json: str, artifact: Artifact | None) -> str:
    """Строка сущности для LLM (чат, ТЗ): текст — кто сказал, кто утвердил · созвон, дата · таймкод.

    ``speaker_source=voice`` помечается «по голосу»: это опознание, а не доказательство дорожкой.
    Старые сущности без привязки выводятся без «кто/когда», но с источником и датой материала.
    """
    from app.app.services.recorded_at import material_date_label
    from app.app.services.transcript_turns import format_timecode

    try:
        value = json.loads(value_json)
    except (json.JSONDecodeError, TypeError):
        value = {"text": value_json}
    if not isinstance(value, dict):
        value = {"text": str(value)}
    line = f"[{entity_type}] {_entity_body(entity_type, value)}"
    who: list[str] = []
    by_voice = " (по голосу)" if value.get("speaker_source") == "voice" else ""
    if value.get("speaker"):
        who.append(f"сказал: {value['speaker']}{by_voice}")
    elif value.get("source_status") == "speaker_unknown":
        who.append("сказал: говорящий не опознан")
    names = [str(d.get("name")) for d in value.get("decided_by") or [] if isinstance(d, dict) and d.get("name")]
    if names:
        who.append(f"утвердил: {', '.join(names)}{by_voice}")
    if who:
        line += " — " + "; ".join(who)
    where: list[str] = []
    if artifact is not None:
        where.append(_source_title(artifact))
        label = material_date_label(artifact)
        if label:
            where.append(label)
    if value.get("start_sec") is not None:
        where.append(format_timecode(value["start_sec"]))
    if where:
        line += " · " + " · ".join(where)
    return line


def artifact_sort_key(artifact: Artifact) -> tuple[float, float, int]:
    upload_ts = 0.0
    if artifact.upload_date:
        upload_ts = artifact.upload_date.timestamp()
    return (-artifact.priority_weight, -upload_ts, -artifact.id)
