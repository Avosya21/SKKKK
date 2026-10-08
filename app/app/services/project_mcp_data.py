"""Данные проектного MCP (только чтение): обзор проекта, материалы целиком порциями, сводка, поиск, сущности, граф, заметки и память решений — то, что отдают инструменты MCP."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.db import get_engine
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity, Link, Resolution, UserNote
from app.app.models.project import Project
from app.app.services.auto_review import verification_for
from app.app.services.graph import build_graph_payload
from app.app.services.ollama_client import embed_texts
from app.app.services.storage import parsed_dir, project_root
from app.app.services.tz import tz_paths
from app.app.services.vector_index import init_vector_store, search_vectors


def _artifact_row(art: Artifact, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Строка списка материалов. Раньше здесь был `art.created_at` (такого поля нет — инструмент падал): дата загрузки
    лежит в `upload_date`; дата СОБЫТИЯ (когда прошёл созвон/переписка) берётся из реестра контракта."""
    meta = meta or {}
    return {
        "artifact_id": art.artifact_id,
        "filename": art.original_filename,
        "file_type": art.file_type,
        "source_type": art.source_type,
        "type": meta.get("type"),
        "status": art.status,
        "transcript_url": art.transcript_html_url,
        "ingested_at": meta.get("ingested_at") or (art.upload_date.isoformat() if art.upload_date else None),
        "occurred_at": meta.get("occurred_at"),
        "occurred_at_precision": meta.get("occurred_at_precision"),
    }


def mcp_project_overview(db: Session, settings: Settings, project_id: int) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise ValueError("Project not found")
    counts = dict(
        db.execute(
            select(Artifact.status, func.count())
            .where(Artifact.project_id == project_id)
            .group_by(Artifact.status)
        ).all()
    )
    done = int(counts.get("done", 0))
    total = sum(int(v) for v in counts.values())
    entities = db.scalar(
        select(func.count())
        .select_from(Entity)
        .join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.is_active.is_(True))
    ) or 0
    from app.app.models.entity import Chunk

    chunks = db.scalar(select(func.count()).select_from(Chunk).where(Chunk.project_id == project_id)) or 0
    tz_path, _ = tz_paths(settings, project_id)
    from app.app.services.exchange_files import catalog_brief

    return {
        "files": catalog_brief(db, project_id),  # посылки файлов проекта (карты, дипмапы); подробно — files_catalog
        "project_id": project_id,
        "name": project.name,
        "description": project.description,
        "artifacts_total": total,
        "artifacts_done": done,
        "entities": entities,
        "indexed_chunks": chunks,
        "has_project_summary": tz_path.is_file() and tz_path.stat().st_size > 0,
    }


def mcp_list_artifacts(
    db: Session,
    project_id: int,
    *,
    status: str | None = None,
    limit: int = 100,
    prefix: str | None = None,
    settings: Settings | None = None,
    updated_since: str | None = None,
) -> list[dict[str, Any]]:
    """Материалы проекта + виртуальные файлы контракта `contract/*.json` (фильтр по префиксу имени: prefix='contract/')."""
    from app.app.services import contract as ctr

    meta: dict[str, dict[str, Any]] = {}
    virtual: list[dict[str, Any]] = []
    if settings is not None:
        built = ctr.get_contract(db, settings, project_id)
        meta = {row["artifact_id"]: row for row in built.files["artifacts"]["items"]}
        state = built.files["state"]
        virtual = [
            {"artifact_id": f"{ctr.CONTRACT_PREFIX}{name}.json", "filename": f"{ctr.CONTRACT_PREFIX}{name}.json", "type": "contract",
             "file_type": "json", "status": "done", "updated_at": state["updated_at"],
             "content_hash": state["file_hashes"].get(f"{ctr.CONTRACT_PREFIX}{name}.json", state["content_hash"]),
             "items": built.files[name].get("total")}
            for name in ctr.FILES
        ]
        virtual.insert(0, {"artifact_id": f"{ctr.CONTRACT_PREFIX}GUIDE.md", "filename": f"{ctr.CONTRACT_PREFIX}GUIDE.md", "type": "contract",
                           "file_type": "md", "status": "done", "updated_at": state["updated_at"], "note": "Начни отсюда: руководство для агента"})
        from app.app.services import agent_gateway as gw
        from app.app.services import agent_letters

        virtual += [{"artifact_id": r["artifact_id"], "filename": r["artifact_id"], "type": "letter", "file_type": "md", "status": "done",
                     "updated_at": r["updated_at"], "content_hash": r["content_hash"], "title": r["title"]} for r in agent_letters.list_letters(project_id)]
        virtual += [
            {"artifact_id": f"{ctr.CONTRACT_PREFIX}{r['name']}", "filename": f"{ctr.CONTRACT_PREFIX}{r['name']}", "type": "kb_reply",
             "file_type": "json", "status": "done", "updated_at": r["at"], "request_id": r["request_id"]}
            for r in gw.list_replies(db, project_id)
        ]
    q = select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.id.desc()).limit(limit)
    if status:
        q = q.where(Artifact.status == status)
    real = [_artifact_row(a, meta.get(a.artifact_id)) for a in db.scalars(q).all()]
    if updated_since:  # только материалы, загруженные/изменённые после метки; файлы контракта сравнивай по content_hash
        real = [r for r in real if str(r.get("updated_at") or r.get("ingested_at") or "") >= updated_since]
    rows = virtual + real
    if prefix:
        rows = [r for r in rows if str(r["filename"] or "").startswith(prefix)]
    return rows


def mcp_get_project_summary(db: Session, settings: Settings, project_id: int) -> dict[str, Any]:
    path, _ = tz_paths(settings, project_id)
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {"project_id": project_id, "path": str(path.relative_to(settings.data_dir)), "content": content}


def mcp_search_project(
    db: Session,
    settings: Settings,
    project_id: int,
    *,
    query: str,
    limit: int = 12,
    text_chars: int = 2000,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Гибридный поиск по фрагментам. Для MCP (`text_chars`=800, `max_bytes`) — короче текст и с указанием, кто и когда сказал;
    полностью материал читается `get_artifact_content` порциями."""
    from app.app.models.entity import Chunk

    if settings.dry_run:
        return {"query": query, "results": []}
    init_vector_store(get_engine())
    vectors = embed_texts(settings, [query])
    if not vectors:
        return {"query": query, "results": []}
    from app.app.services.chunk_fts import init_chunk_fts
    from app.app.services.retrieval import fuse_chunk_hits

    init_chunk_fts(get_engine())
    hits = search_vectors(db, vectors[0], project_id=project_id, limit=limit * 2)
    hits = fuse_chunk_hits(db, query, vectors[0], hits, project_id=project_id, limit=limit,
                           min_score=settings.search_min_score)
    results: list[dict[str, Any]] = []
    for hit in hits:
        art = db.get(Artifact, hit.get("artifact_id"))
        chunk = db.get(Chunk, hit.get("chunk_id")) if hit.get("chunk_id") else None
        text = hit.get("text", "")
        row = {
            "score": hit.get("score"),
            "chunk_id": hit.get("chunk_id"),
            "artifact_id": art.artifact_id if art else None,
            "filename": art.original_filename if art else None,
            "text": text[:text_chars],
            "start_sec": hit.get("start_sec"),
            "end_sec": hit.get("end_sec"),
        }
        if max_bytes is not None:  # MCP: кто и когда, и признак обрезки фрагмента
            row["speaker"] = chunk.speaker if chunk else None
            row["occurred_at"] = art.recorded_at.isoformat() if art and art.recorded_at else None
            row["text_truncated"] = len(text) > text_chars
        results.append(row)
    out: dict[str, Any] = {"query": query, "results": results}
    if max_bytes is not None:
        from app.app.services import mcp_page

        while len(out["results"]) > 1 and mcp_page.size(out) > max_bytes:
            out["results"] = out["results"][:-1]
        if len(out["results"]) < len(results):
            out["note"] = f"показано {len(out['results'])} из {len(results)} фрагментов (порция ответа); уменьши limit или увеличь page_bytes"
    return out


def mcp_list_entities(
    db: Session,
    project_id: int,
    *,
    entity_type: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Сущности порциями `{total, cursor, items}` (раньше — список целиком: 250–550 КБ на живых проектах)."""
    from app.app.services import mcp_page

    q = (
        select(Entity)
        .join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.is_active.is_(True))
        .order_by(Entity.id.desc())
    )
    if entity_type:
        q = q.where(Entity.entity_type == entity_type)
    out: list[dict[str, Any]] = []
    for ent in db.scalars(q).all():
        art = db.get(Artifact, ent.artifact_id)
        try:
            value = json.loads(ent.value_json)
        except json.JSONDecodeError:
            value = ent.value_json
        out.append(
            {
                "id": ent.id,
                "entity_type": ent.entity_type,
                "value": value,
                "artifact_id": art.artifact_id if art else None,
                "filename": art.original_filename if art else None,
                # human_confirmed — человек; model_confirmed — локальная модель по реплике-источнику; extracted — гипотеза
                "verification": verification_for(ent.needs_review, value if isinstance(value, dict) else {}),
                "confidence": ent.confidence,
            }
        )
    return mcp_page.list_page({"items": out}, "items", cursor, max_bytes=mcp_page.current(mcp_page.MAX_PAGE_BYTES), limit=limit)


def mcp_get_knowledge_graph(db: Session, project_id: int, *, limit: int = 200, cursor: str | None = None) -> dict[str, Any]:
    """Граф порциями по узлам: в порции — узлы и рёбра, которые из них выходят (каждое ребро — ровно в одной порции).
    limit — узлов в порции; читать до cursor = null, чтобы собрать граф целиком."""
    from app.app.services import mcp_page

    graph = build_graph_payload(db, project_id, limit=1_000_000)
    edges_by_source: dict[str, list[dict[str, Any]]] = {}
    for edge in graph["edges"]:
        edges_by_source.setdefault(edge["source"], []).append(edge)
    items = [{"node": n, "edges": edges_by_source.get(n["id"], [])} for n in graph["nodes"]]
    page = mcp_page.list_page({"items": items}, "items", cursor, max_bytes=mcp_page.current(mcp_page.MAX_PAGE_BYTES), limit=limit)
    return {"total_nodes": page["total"], "total_edges": len(graph["edges"]), "cursor": page["cursor"],
            "nodes": [i["node"] for i in page["items"]], "edges": [e for i in page["items"] for e in i["edges"]]}


def mcp_list_notes(db: Session, project_id: int, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    from app.app.services import mcp_page

    rows = db.scalars(
        select(UserNote).where(UserNote.project_id == project_id).order_by(UserNote.id.desc())
    ).all()
    items = [
        {
            "id": n.id,
            "text": n.text,
            "author_id": n.user_id,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in rows
    ]
    return mcp_page.list_page({"items": items}, "items", cursor, max_bytes=mcp_page.current(mcp_page.MAX_PAGE_BYTES), limit=limit)


def _entity_brief(ent: Entity | None) -> dict[str, Any] | None:
    """Сущность конфликта разобранным JSON (раньше — строкой JSON внутри JSON) с отметкой, кто её проверил."""
    if ent is None:
        return None
    try:
        value = json.loads(ent.value_json)
    except json.JSONDecodeError:
        value = ent.value_json
    return {"id": ent.id, "entity_type": ent.entity_type, "value": value,
            "verification": verification_for(ent.needs_review, value if isinstance(value, dict) else {})}


def mcp_list_conflicts(db: Session, project_id: int, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    from app.app.services import mcp_page

    links = db.scalars(
        select(Link).where(Link.project_id == project_id, Link.link_type == "POTENTIAL_CONFLICT").order_by(Link.id)
    ).all()
    out: list[dict[str, Any]] = []
    for lnk in links:
        src = db.get(Entity, lnk.source_entity_id)
        tgt = db.get(Entity, lnk.target_entity_id)
        res = db.scalar(select(Resolution).where(Resolution.link_id == lnk.id))
        out.append(
            {
                "link_id": lnk.id,
                "source_entity_id": lnk.source_entity_id,
                "target_entity_id": lnk.target_entity_id,
                "source": _entity_brief(src),
                "target": _entity_brief(tgt),
                "resolved": res is not None,
                "resolution": res.reason if res else None,
                # model_resolved / model_dismissed — решила локальная модель (не человек), человек может вернуть
                "resolved_by": ("model" if res.status in ("model_resolved", "model_dismissed") else "human") if res else None,
            }
        )
    return mcp_page.list_page({"items": out}, "items", cursor, max_bytes=mcp_page.current(mcp_page.MAX_PAGE_BYTES), limit=limit)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


# Что из материала можно прочитать, по виду материала (MCP-KB-PLAN Э1). Раньше читались только файлы созвона, а стенограмма
# молча обрезалась на 120 000 символов: документы и переписки агенту были не видны, длинные созвоны — на треть.
CONTENT_KINDS = ("auto", "summary", "brief", "outline", "text", "transcript", "messages", "entities")
MATERIAL_SOURCES: dict[str, dict[str, str]] = {
    "call": {"summary": "_call_summary.md", "brief": "_call_brief.md", "outline": "_outline.json", "text": "_final.txt"},
    "document": {"summary": "_doc_summary.md", "text": "_clean.txt"},
    "chat": {"summary": "_doc_summary.md", "text": "_clean.txt", "messages": "_messages.json"},
}
AUTO_ORDER = ("summary", "text", "entities")


def material_kind(settings: Settings, art: Artifact) -> str:
    """call — созвон; chat — переписка (есть разобранные сообщения); exchange — файл обмена самого агента
    (analysis_request/question/reply_<n>.json); document — остальное."""
    from app.app.services.agent_gateway import QUESTION_RE, REPLY_RE, REQUEST_RE

    if art.file_type == "audio" or art.source_type == "call":
        return "call"
    name = art.original_filename or ""
    if any(rx.match(name) for rx in (QUESTION_RE, REPLY_RE, REQUEST_RE)):
        return "exchange"
    if (parsed_dir(settings, art.project_id, art.source_type) / f"{art.artifact_id}_messages.json").is_file():
        return "chat"
    return "document"


def _material_text(settings: Settings, art: Artifact, kind: str, mkind: str) -> tuple[str, str]:
    """Текст материала нужного вида: (вид, текст). Пустой текст — такого вида у материала нет."""
    if mkind == "exchange":  # файл обмена агента: его собственное письмо, как прислано
        path = Path(art.file_path)
        return "exchange", _read_text(path) if path.is_file() else ""
    if kind == "transcript":  # прежнее имя: у созвона — стенограмма, у документа и переписки — текст
        kind = "text"
    if kind == "entities":
        return "entities", _read_text(project_root(settings, art.project_id) / "structured" / f"{art.artifact_id}_entities.json")
    suffix = MATERIAL_SOURCES.get(mkind, {}).get(kind)
    text = _read_text(parsed_dir(settings, art.project_id, art.source_type) / f"{art.artifact_id}{suffix}") if suffix else ""
    if not text and kind == "text":
        text = art.transcription_text or ""
    return kind, text


def mcp_get_artifact_content(
    db: Session,
    settings: Settings,
    project_id: int,
    *,
    artifact_id: str,
    kind: str = "auto",
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    from app.app.services import contract as ctr

    if artifact_id.startswith("contract/letters/"):  # письма базы знаний агенту (только чтение)
        from app.app.services import agent_letters

        return agent_letters.get_letter(project_id, artifact_id)
    if artifact_id == f"{ctr.CONTRACT_PREFIX}GUIDE.md":  # руководство для агента
        from app.app.services import agent_guide

        return {"artifact_id": artifact_id, "type": "contract", "kind": "text",
                "content": agent_guide.guide_markdown(project_id, agent_guide.project_name(db, project_id))}
    if artifact_id.startswith(f"{ctr.CONTRACT_PREFIX}kb_reply_"):  # ответ базы знаний на запрос агента анализа
        from app.app.services import agent_gateway as gw

        return {"artifact_id": artifact_id, "type": "kb_reply", "kind": "contract", "content": gw.get_reply(db, project_id, artifact_id)}
    if artifact_id.startswith(ctr.CONTRACT_PREFIX):  # виртуальный файл контракта, порциями с курсором
        page = ctr.get_file(db, settings, project_id, artifact_id, cursor=cursor, limit=limit)
        return {"artifact_id": artifact_id, "type": "contract", "kind": "contract", "content": page}
    art = db.scalar(
        select(Artifact).where(Artifact.project_id == project_id, Artifact.artifact_id == artifact_id)
    )
    if not art:
        raise ValueError("Artifact not found")
    from app.app.services import mcp_page

    if kind not in CONTENT_KINDS:
        raise ValueError(f"kind: один из {', '.join(CONTENT_KINDS)}")
    mkind = material_kind(settings, art)
    available = [k for k in ("summary", "brief", "outline", "text", "messages", "entities") if _material_text(settings, art, k, mkind)[1]]
    frame: dict[str, Any] = {"artifact_id": artifact_id, "filename": art.original_filename, "file_type": art.file_type, "status": art.status,
                             "material": mkind, "available_kinds": available if mkind != "exchange" else ["exchange"]}
    if art.status == "rejected":  # В4: отклонённый материал виден в списке, содержимое не отдаётся
        return {**frame, "kind": kind, "content": "", "reason": f"материал отклонён{': ' + art.error_message if art.error_message else ''}"}
    used, content = kind, ""
    for key in (AUTO_ORDER if kind == "auto" else (kind,)):
        used, content = _material_text(settings, art, key, mkind)
        if content:
            break
    if not content:
        why = "материал ещё обрабатывается" if art.status != "done" else f"у материала нет вида {kind}"
        return {**frame, "kind": used, "content": "", "reason": f"{why}; есть: {', '.join(frame['available_kinds']) or 'ничего'}"}
    # порция по бюджету вызова (page_bytes); limit здесь не действует — он для файлов контракта (число пунктов)
    return mcp_page.text_page(content, cursor, {**frame, "kind": used}, max_bytes=mcp_page.current(mcp_page.MAX_PAGE_BYTES))


def mcp_get_decisions(
    db: Session,
    settings: Settings,
    project_id: int,
    *,
    subject_id: int | None = None,
    as_of: str | None = None,
    query: str | None = None,
    state: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Память решений (MCP-KB-PLAN Э5): что решили, кто и когда сказал, что отклонили или заменили. Только в режиме live (В2),
    как и для чата портала: в shadow память видна лишь администратору и ещё проверяется.

    Без параметров — предметы проекта; query — предметы по смыслу вопроса; subject_id — лента реплик и журнал предмета
    (as_of — итог на дату); as_of без subject_id — состояние всех предметов на дату. Всё порциями с курсором."""
    from app.app.services import decision_memory_view as view
    from app.app.services import mcp_page, memory_mode

    project = db.get(Project, project_id)
    mode = memory_mode.mode_of(settings, project)
    if mode != "live":
        raise PermissionError(f"память решений для проекта ещё не включена (режим {mode}): она открывается агентам вместе с чатом портала")
    budget = mcp_page.current(mcp_page.MAX_PAGE_BYTES)
    if as_of and not re.match(r"^\d{4}-\d{2}-\d{2}$", as_of):
        raise ValueError("as_of: дата ГГГГ-ММ-ДД")
    if subject_id is not None:
        s = view.active_subject(db, project_id, int(subject_id))
        if s is None:
            raise ValueError("Предмет не найден")
        detail = view.subject_detail(db, project_id, s, as_of=as_of)
        return mcp_page.list_page(detail, "acts", cursor, max_bytes=budget)
    if as_of:
        return mcp_page.list_page(view.state_at(db, project_id, as_of), "items", cursor, max_bytes=budget)
    ids = None
    if query and query.strip():
        from app.app.services.memory_chat import pick_subjects

        try:
            vectors = embed_texts(settings, [query]) if not settings.dry_run else []
        except Exception:  # noqa: BLE001 — без векторов ищем по словам
            vectors = []
        ids = [s.id for s in pick_subjects(db, project_id, vectors[0] if vectors else None, query, limit=30)]
    listing = view.list_subjects(db, project_id, state=state, ids=ids, limit=None)
    page = mcp_page.list_page(listing, "items", cursor, max_bytes=budget)
    page["hint"] = "лента предмета с цитатами — get_decisions(subject_id); итог на дату — as_of=ГГГГ-ММ-ДД"
    return page
