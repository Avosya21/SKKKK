"""Граф знаний проекта: тексты сущностей, автосвязи по косинусной близости, пересборка графа, удаление сущностей с зависимостями, чистка «осиротевших» связей и автоматическое разрешение конфликтов."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.db import get_engine
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity, Link, NoteRef, Resolution
from app.app.services.conflict_rules import CANCEL_PATTERNS as _CANCEL_PATTERNS
from app.app.services.ollama_client import embed_texts
from app.app.services.vector_index import (
    init_vector_store,
    load_entity_vectors,
    upsert_entity_vectors,
)

logger = logging.getLogger("primaryanalitics.graph")

# Шаблон явной отмены — в conflict_rules (общий с проверкой конфликтов моделью); здесь реэкспорт для старых вызовов.
CANCEL_PATTERNS = _CANCEL_PATTERNS


CONFLICT_ENTITY_TYPES = frozenset({"decision", "task", "fact"})

# Ключи «кто / когда / откуда» в value_json. В строку для эмбеддинга и сравнения «то же ли решение» не входят:
# иначе одинаковое решение из двух созвонов (разные говорящий и время) стало бы POTENTIAL_CONFLICT.
PROVENANCE_KEYS = frozenset(
    {
        "speaker",
        "speaker_source",
        "decided_by",
        "start_sec",
        "end_sec",
        "turns",
        "source_span_id",
        "text_hash",
        "recorded_at",
        "source_status",
        "artifact_id",
        "source_type",
        "needs_review",
        "human_review",
        "model_review",
    }
)


def entity_text(value_json: str) -> str:
    """Канон строки для bge-m3: только смысловые поля. Persist и seed должны считать одно и то же."""
    try:
        data = json.loads(value_json)
    except json.JSONDecodeError:
        return value_json
    if isinstance(data, dict):
        return " ".join(str(v) for k, v in data.items() if k not in PROVENANCE_KEYS)
    return str(data)


def _entity_text(entity: Entity) -> str:
    return entity_text(entity.value_json)


def delete_entities_with_deps(db: Session, entity_ids: list[int]) -> None:
    """Снять resolutions/links/note_refs, затем строки entities. Иначе SQLite FK падает."""
    if not entity_ids:
        return
    link_ids = list(
        db.scalars(
            select(Link.id).where(
                or_(Link.source_entity_id.in_(entity_ids), Link.target_entity_id.in_(entity_ids))
            )
        )
    )
    if link_ids:
        db.execute(delete(Resolution).where(Resolution.link_id.in_(link_ids)))
        db.execute(delete(Link).where(Link.id.in_(link_ids)))
    db.execute(
        delete(Resolution).where(
            or_(
                Resolution.winning_entity_id.in_(entity_ids),
                Resolution.losing_entity_id.in_(entity_ids),
            )
        )
    )
    db.execute(delete(NoteRef).where(NoteRef.target_kind == "entity", NoteRef.target_id.in_(entity_ids)))
    db.execute(delete(Entity).where(Entity.id.in_(entity_ids)))


def purge_orphan_links(db: Session, *, project_id: int | None = None) -> int:
    """Удалить связи, у которых уже нет source/target сущности (после reextract без FK)."""
    live_ids = select(Entity.id)
    q = select(Link).where(
        or_(
            Link.source_entity_id.not_in(live_ids),
            Link.target_entity_id.not_in(live_ids),
        )
    )
    if project_id is not None:
        q = q.where(Link.project_id == project_id)
    rows = list(db.scalars(q).all())
    if not rows:
        return 0
    link_ids = [row.id for row in rows]
    db.execute(delete(Resolution).where(Resolution.link_id.in_(link_ids)))
    db.execute(delete(Link).where(Link.id.in_(link_ids)))
    log_event(logger, "graph.orphan_links.purged", count=len(link_ids), project_id=project_id)
    return len(link_ids)


def rebuild_project_graph(db: Session, settings: Settings, project_id: int) -> dict[str, int]:
    """Пересобрать автоматические связи по текущим сущностям проекта."""
    from app.app.services.storage import artifact_knowledge_text

    purged = purge_orphan_links(db, project_id=project_id)
    auto_ids = list(
        db.scalars(
            select(Link.id).where(Link.project_id == project_id, Link.is_automatic.is_(True))
        ).all()
    )
    # Решённые связи (человеком или моделью — в том числе замены по теме, REQUIREMENTS-PLAN этап 7) переживают
    # пересборку: иначе проигравший остаётся выключенным, а причина и «Вернуть» пропадают.
    resolved_links = select(Resolution.link_id)
    auto_ids = list(db.scalars(select(Link.id).where(Link.id.in_(auto_ids), Link.id.not_in(resolved_links))).all()) if auto_ids else []
    if auto_ids:
        db.execute(delete(Resolution).where(Resolution.link_id.in_(auto_ids)))
        db.execute(delete(Link).where(Link.id.in_(auto_ids)))
    artifacts = db.scalars(
        select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.id.asc())
    ).all()
    seeded = 0
    for art in artifacts:
        has_ents = db.scalar(
            select(Entity.id).where(Entity.artifact_id == art.id, Entity.is_active.is_(True)).limit(1)
        )
        if not has_ents:
            continue
        text = artifact_knowledge_text(settings, art) or art.transcription_text or ""
        seed_graph(db, settings, art, text)
        seeded += 1
    db.commit()
    left = db.scalar(select(func.count()).select_from(Link).where(Link.project_id == project_id)) or 0
    return {"purged_orphans": purged, "artifacts_seeded": seeded, "links": int(left)}


LEGACY_AUTO_REASON = "auto: explicit cancellation phrase in newer artifact"


def repair_conflict_links(db: Session, project_id: int, *, apply: bool = False, release_legacy: bool = False) -> dict[str, Any]:
    """Разовая чистка последствий ошибок О1–О5 (CONFLICT-AUTONOMY-PLAN этап 1). Без ``apply`` — только отчёт.

    - Обратные дубли A→B / B→A одной пары: остаётся связь с решением (человека — в первую очередь), иначе с меньшим id.
    - ``release_legacy``: снять старые авторазрешения ``_try_auto_resolve`` (фраза отмены в любом месте текста, победитель —
      по направлению связи) и включить проигравшего, если его не подтверждал человек и он не проиграл в другом решении.
      Конфликт возвращается в очередь и проходит новую проверку (правила + модель).
    """
    report: dict[str, Any] = {"project_id": project_id, "duplicate_pairs": 0, "duplicate_links_removed": 0,
                              "legacy_auto": [], "legacy_released": 0, "applied": apply}
    links = db.scalars(select(Link).where(Link.project_id == project_id, Link.link_type == "POTENTIAL_CONFLICT").order_by(Link.id)).all()
    groups: dict[tuple[int, int], list[Link]] = {}
    for link in links:
        key = tuple(sorted((link.source_entity_id, link.target_entity_id)))
        groups.setdefault(key, []).append(link)
    for group in groups.values():
        if len(group) < 2:
            continue
        report["duplicate_pairs"] += 1
        res = {lk.id: db.scalar(select(Resolution).where(Resolution.link_id == lk.id)) for lk in group}
        keep = sorted(group, key=lambda lk: (res[lk.id] is None, not (res[lk.id] and res[lk.id].resolved_by), lk.id))[0]
        for lk in group:
            if lk.id == keep.id:
                continue
            report["duplicate_links_removed"] += 1
            if apply:
                if res[lk.id] is not None:
                    db.delete(res[lk.id])
                db.delete(lk)
    legacy = db.scalars(
        select(Resolution).join(Link, Link.id == Resolution.link_id)
        .where(Link.project_id == project_id, Resolution.reason == LEGACY_AUTO_REASON)
    ).all()
    for res in legacy:
        loser = db.get(Entity, res.losing_entity_id)
        try:
            human = bool(json.loads(loser.value_json or "{}").get("human_review")) if loser else False
        except (json.JSONDecodeError, AttributeError):
            human = False
        other_loss = db.scalar(select(func.count()).select_from(Resolution).where(
            Resolution.losing_entity_id == res.losing_entity_id, Resolution.id != res.id)) or 0
        report["legacy_auto"].append({"link_id": res.link_id, "winner": res.winning_entity_id, "loser": res.losing_entity_id,
                                      "loser_human": human, "loser_lost_elsewhere": bool(other_loss)})
        if apply and release_legacy:
            db.delete(res)
            if loser is not None and not human and not other_loss:
                loser.is_active = True
            report["legacy_released"] += 1
    if apply:
        db.flush()
    log_event(logger, "graph.conflicts.repaired", project=project_id, dupes=report["duplicate_links_removed"],
              legacy=len(report["legacy_auto"]), released=report["legacy_released"], applied=apply)
    return report


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def vectors_for_entities(
    db: Session, settings: Settings, entities: list[Entity]
) -> tuple[list[list[float]], int]:
    if not entities:
        return [], 0
    loaded = load_entity_vectors(db, [e.id for e in entities])
    missing = [e for e in entities if e.id not in loaded]
    embedded = 0
    if missing and not settings.dry_run:
        vecs = embed_texts(settings, [entity_text(e.value_json) for e in missing])
        pairs = list(zip((e.id for e in missing), vecs, strict=True))
        upsert_entity_vectors(db, pairs)
        for eid, vec in pairs:
            loaded[eid] = vec
        embedded = len(missing)
    return [loaded[e.id] for e in entities], embedded


def seed_graph(db: Session, settings: Settings, artifact: Artifact, source_text: str) -> None:
    init_vector_store(get_engine())
    threshold = artifact.project.settings().get("similarity_threshold", settings.similarity_threshold)
    new_entities = db.scalars(select(Entity).where(Entity.artifact_id == artifact.id, Entity.is_active.is_(True))).all()
    if not new_entities:
        log_event(logger, "graph.skipped", artifact=artifact.artifact_id, reason="no active entities")
        return

    new_vecs, emb_new = vectors_for_entities(db, settings, list(new_entities))

    prior = db.scalars(
        select(Entity)
        .join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == artifact.project_id, Entity.artifact_id != artifact.id, Entity.is_active.is_(True))
    ).all()
    prior_list = list(prior)
    prior_vecs, emb_prior = vectors_for_entities(db, settings, prior_list) if prior_list else ([], 0)

    created: dict[str, int] = {}
    for ent, vec in zip(new_entities, new_vecs, strict=True):
        for old, old_vec in zip(prior_list, prior_vecs, strict=True):
            score = _cosine(vec, old_vec)
            if score < threshold:
                continue
            same_type = ent.entity_type == old.entity_type
            same_value = _entity_text(ent).strip().lower() == _entity_text(old).strip().lower()
            # Противоречить могут только решения, задачи и факты. Пары участников / упоминаний с разными именами
            # раньше давали половину всех конфликтов (30 из 59 на 2026-09-24) — это не противоречие.
            if same_type and not same_value and ent.entity_type in CONFLICT_ENTITY_TYPES:
                link_type = "POTENTIAL_CONFLICT"
            else:
                link_type = "SEMANTICALLY_RELATED"
            # Связь без направления: при пересборке пара A↔B встречается дважды (A сравнивается с B и B с A).
            exists = db.scalar(
                select(Link.id).where(
                    Link.project_id == artifact.project_id,
                    or_(
                        (Link.source_entity_id == ent.id) & (Link.target_entity_id == old.id),
                        (Link.source_entity_id == old.id) & (Link.target_entity_id == ent.id),
                    ),
                    Link.link_type == link_type,
                )
            )
            if exists:
                continue
            db.add(
                Link(
                    project_id=artifact.project_id,
                    source_entity_id=ent.id,
                    target_entity_id=old.id,
                    link_type=link_type,
                    similarity_score=score,
                    is_automatic=True,
                    description=None,
                )
            )
            created[link_type] = created.get(link_type, 0) + 1

    compared = len(prior_list)
    total = len(new_entities) + compared
    embedded = emb_new + emb_prior
    log_event(
        logger,
        "graph.seeded",
        artifact=artifact.artifact_id,
        entities=len(new_entities),
        compared_with=compared,
        related=created.get("SEMANTICALLY_RELATED", 0),
        conflicts=created.get("POTENTIAL_CONFLICT", 0),
        threshold=round(threshold, 2),
        cached=max(0, total - embedded),
        embedded=embedded,
    )
    # Сессия работает с autoflush=False, поэтому только что добавленные связи
    # не попадут в выборку авторазрешения без явного flush.
    db.flush()
    _try_auto_resolve(db, settings, artifact, source_text)


def _try_auto_resolve(db: Session, settings: Settings, artifact: Artifact, source_text: str) -> None:
    """Явная отмена без модели (правило ``explicit_cancel``, CONFLICT-AUTONOMY-PLAN этап 1).

    Только конфликты этого артефакта; «новее» — по дате материала и таймкоду, не по направлению связи; фраза отмены —
    в источнике более нового пункта (реплики вокруг него / абзац), а не где угодно в тексте; отменяющий пункт не слабее
    отменяемого (Р1); сторону, подтверждённую человеком, не трогает (проверка в ``apply_conflict_verdict``).
    Решение отличимо от человеческого и отменяется «Вернуть», как решение модели.
    """
    if not CANCEL_PATTERNS.search(source_text or ""):
        return
    from app.app.services.auto_review import MODE_LIVE
    from app.app.services.review_conflicts import _pair, apply_conflict_verdict, effective_mode, rule_before_model

    mode = effective_mode(artifact.project, MODE_LIVE)
    ids = select(Entity.id).where(Entity.artifact_id == artifact.id)
    conflicts = db.scalars(
        select(Link).where(
            Link.project_id == artifact.project_id,
            Link.link_type == "POTENTIAL_CONFLICT",
            Link.is_automatic.is_(True),
            or_(Link.source_entity_id.in_(ids), Link.target_entity_id.in_(ids)),
        )
    ).all()
    resolved = 0
    cache: dict = {}
    for link in conflicts:
        if db.scalar(select(Resolution.id).where(Resolution.link_id == link.id)):
            continue
        pair = _pair(db, settings, link, cache)
        if pair is None or not pair["material"] or pair["newer"]["artifact_id"] != artifact.id:
            continue
        decided = rule_before_model(pair)
        if decided is None:
            continue
        verdict = apply_conflict_verdict(
            db, settings, pair, verdict=decided["verdict"], confidence=1.0, mode=mode, model=decided["rule"],
            rule=decided["rule"], reason=decided["reason"], relation=decided["relation"],
        )
        resolved += verdict == "newer_wins"

    if resolved:
        log_event(
            logger,
            "graph.conflicts.auto_resolved",
            artifact=artifact.artifact_id,
            resolved=resolved,
            mode=mode,
        )


def build_graph_payload(db: Session, project_id: int, *, limit: int = 200) -> dict[str, Any]:
    artifacts = list(
        db.scalars(select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.id.desc())).all()
    )
    entities = list(
        db.scalars(
            select(Entity)
            .join(Artifact, Artifact.id == Entity.artifact_id)
            .where(Artifact.project_id == project_id, Entity.is_active.is_(True))
            .order_by(Entity.id.desc())
            .limit(limit)
        ).all()
    )
    node_ids = {f"artifact-{art.id}" for art in artifacts} | {f"entity-{ent.id}" for ent in entities}
    allowed_entity_ids = {ent.id for ent in entities}
    links = db.scalars(select(Link).where(Link.project_id == project_id)).all()

    nodes: list[dict[str, Any]] = []
    for art in artifacts:
        nodes.append(
            {
                "id": f"artifact-{art.id}",
                "kind": "artifact",
                "label": art.original_filename,
                "artifact_id": art.artifact_id,
            }
        )
    for ent in entities:
        nodes.append(
            {
                "id": f"entity-{ent.id}",
                "kind": "entity",
                "label": f"{ent.entity_type}",
                "entity_type": ent.entity_type,
                "needs_review": ent.needs_review,
            }
        )
    edges = []
    for lnk in links:
        if lnk.source_entity_id not in allowed_entity_ids or lnk.target_entity_id not in allowed_entity_ids:
            continue
        src = f"entity-{lnk.source_entity_id}"
        tgt = f"entity-{lnk.target_entity_id}"
        if src not in node_ids or tgt not in node_ids:
            continue
        edges.append(
            {
                "id": f"link-{lnk.id}",
                "source": src,
                "target": tgt,
                "type": lnk.link_type,
                "score": lnk.similarity_score,
                "automatic": lnk.is_automatic,
            }
        )
    for ent in entities:
        edges.append(
            {
                "id": f"contains-{ent.id}",
                "source": f"artifact-{ent.artifact_id}",
                "target": f"entity-{ent.id}",
                "type": "CONTAINS",
                "automatic": True,
            }
        )
    return {"nodes": nodes, "edges": edges}
