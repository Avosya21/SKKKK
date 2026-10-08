"""Чат с ИИ по материалам проекта: история и указания пользователя, уточняющие вопросы, поиск фрагментов и сущностей, сборка контекста, ответ LLM с источниками и ожидающие сообщения (`answer_chat`)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.db import get_engine
from app.app.models.artifact import Artifact
from app.app.models.chat_guidance import ChatGuidance
from app.app.models.chat_message import ChatMessage
from app.app.models.entity import Entity, Link, UserNote
from app.app.models.job import Job
from app.app.services.llm import chat_completion_no_think
from app.app.services.source_links import source_url
from app.app.services.artifact_reports import load_document_payload
from app.app.services.summarize_document import load_document_source_text, load_document_summary_md
from app.app.services.chunk_fts import init_chunk_fts
from app.app.services.ollama_client import embed_texts
from app.app.services.storage import parsed_dir
from app.app.services.recorded_at import material_date_label
from app.app.services.retrieval import (
    ENTITY_SEARCH_LIMIT,
    chunk_header,
    enrich_and_rerank_hits,
    format_entity_line,
    fuse_chunk_hits,
    search_entities,
    weighted_score,
)
from app.app.services.vector_index import init_vector_store, search_vectors
from app.app.services.prompts import load_prompt_block

logger = logging.getLogger("skkkk.chat")
ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "rag_chat.md"
CHAT_SEARCH_LIMIT = 12
CHAT_HISTORY_LIMIT = 1000
CHAT_PENDING_TEXT = "Думаю над ответом..."
CHAT_STALE_MINUTES = 30
CHAT_DOC_EXCERPT_CHARS = 3500
# Если у документа есть выжимка, исходного текста в промпт кладём меньше: контекст чата доходил до 20 тысяч токенов,
# а почти всё время ответа уходит на обработку промпта (замеры 21.09: ~28 с в среднем, поиск и БД — меньше секунды).
CHAT_DOC_EXCERPT_WITH_SUMMARY_CHARS = 1800
# Начало документа + фрагменты этого документа, найденные по словам вопроса (таблица в конце файла иначе не видна).
CHAT_DOC_HEAD_WITH_MATCHES_CHARS = 1000
CHAT_DOC_MATCH_LIMIT = 3
CHAT_DOC_KEYWORDS = ("директор", "вопрос", "последн", "документ", "файл", "заметк", "txt")
CHAT_PRIORITY_KEYWORDS = ("приоритет", "актуальн", "последн", "главн", "важнее", "считать")
CLARIFICATION_MARKER = "🤔 Нужно ваше уточнение"
CHAT_MATERIAL_ENTITIES = 15  # пункты найденных материалов в допконтексте — ближайшие к вопросу
# Допконтекст без блока «что в силе»: карточка, пункты, заметки, выжимки созвонов. Блок изменений решений идёт первым и
# не режется — раньше он стоял в конце и отрезался лимитом (урок 2026-09-28: начинался с 10 764-го знака из 8 000).
CHAT_SUPPLEMENT_CHARS = 8000
DOC_DUPLICATE_PROBE = 150  # начало фрагмента поиска, по которому видно, что он уже есть в блоке документа


def _squash(text: str) -> str:
    return " ".join((text or "").split())


def asr_busy(db: Session) -> bool:
    row = db.scalar(select(Job.id).where(Job.status == "running", Job.phase == "asr").limit(1))
    return row is not None


def _load_prompt() -> str:
    """Системный промпт чата — код-блок из rag_chat.md, а не markdown-заголовок
    перед ним (blocks[0] был заголовком файла — реальные правила чата
    ассистент никогда не получал). Блок хранит и шаблон пользовательского
    сообщения ({question}/{chunks}/{graph_context}) — код собирает user
    отдельно (другая структура секций), поэтому берём только инструкции до
    этого шаблона, чтобы не слать модели незаполненные плейсхолдеры."""
    block = load_prompt_block(PROMPT_PATH)
    instructions, _, _ = block.partition("\n\nВопрос: {question}")
    return instructions.strip() or block


def _parse_meta(row: ChatMessage) -> dict[str, Any]:
    if not row.meta_json:
        return {}
    try:
        data = json.loads(row.meta_json)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _dump_meta(meta: dict[str, Any] | None) -> str | None:
    if not meta:
        return None
    return json.dumps(meta, ensure_ascii=False)


def list_active_guidance(db: Session, *, project_id: int, user_id: int, limit: int = 12) -> list[str]:
    rows = db.scalars(
        select(ChatGuidance)
        .where(
            ChatGuidance.project_id == project_id,
            ChatGuidance.user_id == user_id,
            ChatGuidance.is_active.is_(True),
        )
        .order_by(ChatGuidance.id.desc())
        .limit(limit)
    ).all()
    return [row.text.strip() for row in rows if row.text.strip()]


def save_chat_guidance(
    db: Session,
    *,
    project_id: int,
    user_id: int,
    text: str,
    topic: str | None = None,
) -> ChatGuidance:
    row = ChatGuidance(
        project_id=project_id,
        user_id=user_id,
        topic=topic,
        text=text.strip(),
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _pending_clarification_for_reply(db: Session, *, project_id: int, user_id: int) -> dict[str, Any] | None:
    last_assistant = db.scalar(
        select(ChatMessage)
        .where(
            ChatMessage.project_id == project_id,
            ChatMessage.user_id == user_id,
            ChatMessage.role == "assistant",
            ChatMessage.waiting.is_(False),
        )
        .order_by(ChatMessage.id.desc())
        .limit(1)
    )
    if not last_assistant:
        return None
    meta = _parse_meta(last_assistant)
    if meta.get("kind") != "clarification":
        return None
    answered = db.scalar(
        select(ChatMessage.id).where(
            ChatMessage.project_id == project_id,
            ChatMessage.user_id == user_id,
            ChatMessage.role == "user",
            ChatMessage.id > last_assistant.id,
        ).limit(1)
    )
    if answered:
        return None
    return meta


def _question_needs_priority_choice(question: str) -> bool:
    q = question.lower()
    return any(k in q for k in CHAT_PRIORITY_KEYWORDS)


def _question_about_project_priority(question: str) -> bool:
    """Вопрос о текущем фокусе/приоритете проекта — отвечаем, не просим выбрать сущность."""
    q = question.lower()
    if "приоритет" not in q:
        return False
    if "проект" in q:
        return True
    return "сейчас" in q and "актуальн" in q


def _competing_source_groups(hit_artifacts: list[Artifact]) -> tuple[list[Artifact], list[Artifact]]:
    docs = [a for a in hit_artifacts if a.source_type == "document"]
    calls = [a for a in hit_artifacts if a.source_type == "call" or a.file_type == "audio"]
    return docs, calls


def detect_chat_ambiguity(
    db: Session,
    *,
    project_id: int,
    question: str,
    hit_artifacts: list[Artifact],
    guidance_texts: list[str],
) -> dict[str, Any] | None:
    # Р4 (DECISIONS 2026-09-25): открытое противоречие не останавливает чат — обе версии с датами идут в контекст
    # (_supplement_context → conflict_context), модель называет обе и какая позже. Уточнение — только source_split.
    docs, calls = _competing_source_groups(hit_artifacts)
    wants_priority = _question_needs_priority_choice(question)
    project_priority_q = _question_about_project_priority(question)

    if project_priority_q:
        return None

    if wants_priority and docs and calls:
        doc_weight = max(a.priority_weight for a in docs)
        call_weight = max(a.priority_weight for a in calls)
        if doc_weight > call_weight + 0.05:
            return None
        combined_guidance = " ".join(guidance_texts).lower()
        if any(doc.original_filename.lower() in combined_guidance for doc in docs):
            return None
        if any(call.original_filename.lower() in combined_guidance for call in calls):
            return None
        return {
            "reason": "source_split",
            "documents": [
                {"artifact_id": a.artifact_id, "filename": a.original_filename, "priority_weight": a.priority_weight}
                for a in docs[:3]
            ],
            "calls": [
                {"artifact_id": a.artifact_id, "filename": a.original_filename, "priority_weight": a.priority_weight}
                for a in calls[:3]
            ],
        }
    return None


def _build_clarification_answer(question: str, ambiguity: dict[str, Any]) -> str:
    lines = [CLARIFICATION_MARKER, "", "Не могу однозначно выбрать, на что опираться:", ""]

    if ambiguity["reason"] == "source_split":
        lines.append("Нашлись и **документы**, и **созвоны** по теме:")
        for doc in ambiguity.get("documents", []):
            lines.append(
                f"- документ **{doc['filename']}** (artifact_id={doc['artifact_id']}, вес {doc['priority_weight']})"
            )
        for call in ambiguity.get("calls", []):
            lines.append(
                f"- созвон **{call['filename']}** (artifact_id={call['artifact_id']}, вес {call['priority_weight']})"
            )
        lines.append("")

    lines.extend(
        [
            f"**Вопрос был:** {question}",
            "",
            "Ответьте одним сообщением, что считать актуальным (например: «берём документ от 19.08, созвон — фон»).",
            "Это сохранится как указание для чата и не изменит граф проекта автоматически.",
        ]
    )
    return "\n".join(lines)


def list_chat_messages(
    db: Session,
    *,
    project_id: int,
    user_id: int,
    limit: int = CHAT_HISTORY_LIMIT,
) -> list[dict[str, Any]]:
    cleanup_stale_chat_pending(db, project_id=project_id, user_id=user_id)
    newest = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.project_id == project_id, ChatMessage.user_id == user_id)
        .order_by(ChatMessage.id.desc())
        .limit(limit)
    ).all()
    rows = list(reversed(newest))
    out: list[dict[str, Any]] = []
    for row in rows:
        sources: list[dict[str, Any]] = []
        if row.sources_json:
            try:
                sources = json.loads(row.sources_json)
            except json.JSONDecodeError:
                sources = []
        out.append(
            {
                "id": row.id,
                "role": row.role,
                "content": row.content,
                "sources": sources,
                "waiting": row.waiting,
                "meta": _parse_meta(row),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    return out


def save_chat_message(
    db: Session,
    *,
    project_id: int,
    user_id: int,
    role: str,
    content: str,
    sources: list[dict[str, Any]] | None = None,
    waiting: bool = False,
    meta: dict[str, Any] | None = None,
) -> ChatMessage:
    row = ChatMessage(
        project_id=project_id,
        user_id=user_id,
        role=role,
        content=content,
        sources_json=json.dumps(sources or [], ensure_ascii=False) if sources is not None else None,
        meta_json=_dump_meta(meta),
        waiting=waiting,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _log_chat_activity(
    db: Session,
    *,
    project_id: int,
    user_id: int,
    action: str,
    title: str,
    content: str,
) -> None:
    from app.app.services.user_activity import BLOCK_CHAT, log_activity

    log_activity(
        db,
        user_id=user_id,
        project_id=project_id,
        block=BLOCK_CHAT,
        action=action,
        title=title,
        content=content,
    )


def finalize_chat_message(
    db: Session,
    row: ChatMessage,
    *,
    content: str,
    sources: list[dict[str, Any]] | None = None,
    waiting: bool = False,
    meta: dict[str, Any] | None = None,
) -> ChatMessage:
    row.content = content
    row.waiting = waiting
    if sources is not None:
        row.sources_json = json.dumps(sources, ensure_ascii=False)
    if meta is not None:
        row.meta_json = _dump_meta(meta)
    db.commit()
    db.refresh(row)
    if not waiting and content.strip() and content.strip() != CHAT_PENDING_TEXT:
        from app.app.services.user_activity import ACTION_ANSWER

        _log_chat_activity(
            db,
            project_id=row.project_id,
            user_id=row.user_id,
            action=ACTION_ANSWER,
            title="Ответ ИИ",
            content=content,
        )
    return row


def cleanup_stale_chat_pending(
    db: Session,
    *,
    project_id: int | None = None,
    user_id: int | None = None,
) -> int:
    """Снять зависшие waiting-сообщения после обрыва запроса."""
    now = datetime.now(timezone.utc)
    q = select(ChatMessage).where(
        ChatMessage.role == "assistant",
        ChatMessage.waiting.is_(True),
    )
    if project_id is not None:
        q = q.where(ChatMessage.project_id == project_id)
    if user_id is not None:
        q = q.where(ChatMessage.user_id == user_id)
    rows = db.scalars(q).all()
    n = 0
    for row in rows:
        created = row.created_at
        if not created:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if now - created > timedelta(minutes=CHAT_STALE_MINUTES):
            row.waiting = False
            row.content = "Ответ не получен — запрос прерван или превышено время ожидания."
            n += 1
    if n:
        db.commit()
    return n


def chat_is_pending(db: Session, *, project_id: int, user_id: int) -> bool:
    cleanup_stale_chat_pending(db, project_id=project_id, user_id=user_id)
    row = db.scalar(
        select(ChatMessage.id).where(
            ChatMessage.project_id == project_id,
            ChatMessage.user_id == user_id,
            ChatMessage.role == "assistant",
            ChatMessage.waiting.is_(True),
        ).limit(1)
    )
    return row is not None


def _graph_snippet(db: Session, project_id: int, entity_ids: list[int]) -> str:
    if not entity_ids:
        return ""
    entities = db.scalars(select(Entity).where(Entity.id.in_(entity_ids))).all()
    by_id = {e.id: e for e in entities}
    # Противоречия (POTENTIAL_CONFLICT) здесь не выводятся: их со статусом «в силе / заменено / не решено»
    # даёт conflict_context в _supplement_context. Сырой value_json модели не показываем — только строки сущностей.
    links = db.scalars(
        select(Link).where(
            Link.project_id == project_id,
            Link.link_type != "POTENTIAL_CONFLICT",
            or_(
                Link.source_entity_id.in_(entity_ids),
                Link.target_entity_id.in_(entity_ids),
            ),
        )
    ).all()

    def line(ent: Entity) -> str:
        return format_entity_line(ent.entity_type, ent.value_json, ent.artifact)

    lines = ["Соседи графа:"]
    for ent_id in entity_ids[:12]:
        ent = by_id.get(ent_id)
        if ent:
            lines.append(f"- {line(ent)}")
    for lnk in links[:20]:
        src = by_id.get(lnk.source_entity_id)
        tgt = by_id.get(lnk.target_entity_id)
        if src is None or tgt is None:
            continue
        lines.append(f"- {lnk.link_type}: {line(src)} -> {line(tgt)}")
    return "\n".join(lines)


def _entities_for_artifacts(
    db: Session,
    artifact_db_ids: set[int],
    query_vec: list[float] | None = None,
    *,
    exclude: set[int] | None = None,
    min_score: float = 0.0,
    limit: int = CHAT_MATERIAL_ENTITIES,
) -> list[Entity]:
    """Пункты найденных материалов, ближайшие к вопросу; без вектора — после них, по номеру. Раньше — первые 40 по номеру:
    у созвона это участники и организационные мелочи, а пункты по теме вопроса до модели не доходили (урок 2026-09-28)."""
    if not artifact_db_ids:
        return []
    skip = exclude or set()
    rows = [
        e
        for e in db.scalars(
            select(Entity).where(Entity.artifact_id.in_(artifact_db_ids), Entity.is_active.is_(True)).order_by(Entity.id)
        ).all()
        if e.id not in skip
    ]
    if query_vec is None:
        return rows[:limit]
    from app.app.services.graph import _cosine
    from app.app.services.vector_index import load_entity_vectors

    vectors = load_entity_vectors(db, [e.id for e in rows])
    scored = sorted(((_cosine(query_vec, vectors[e.id]), e) for e in rows if e.id in vectors), key=lambda row: -row[0])
    ranked = [e for score, e in scored if score >= min_score]
    return (ranked + [e for e in rows if e.id not in vectors])[:limit]


def _load_document_raw_excerpt(settings: Settings, artifact: Artifact, *, max_chars: int = CHAT_DOC_EXCERPT_CHARS) -> str:
    return load_document_source_text(settings, artifact)[:max_chars].strip()


def _document_relevance_score(question: str, artifact: Artifact, raw_excerpt: str) -> float:
    q = question.lower()
    fn = (artifact.original_filename or "").lower()
    raw_head = raw_excerpt[:400].lower()
    score = 0.0
    if "вопрос" in fn:
        score += 4.0
    if "директор" in fn:
        score += 2.0
    if raw_head.startswith("вопрос от директора") or "вопрос от директора" in raw_head:
        score += 5.0
    if any(k in q for k in CHAT_DOC_KEYWORDS):
        score += 1.5
    if "директор" in raw_excerpt.lower():
        score += 1.0
    if "последн" in q and ("вопрос" in fn or "вопрос" in raw_head):
        score += 2.0
    score += artifact.priority_weight * 2.0
    return score


def _document_question_matches(db: Session, project_id: int, artifact: Artifact, question: str, shown: str) -> list[str]:
    """Фрагменты документа по словам и числам вопроса (FTS), которых нет в уже показанном начале."""
    from app.app.services.chunk_fts import search_chunk_fts

    hits = search_chunk_fts(db, question, project_id=project_id, limit=CHAT_DOC_MATCH_LIMIT + 2, artifact_id=artifact.id)
    out: list[str] = []
    for hit in hits:
        body = (hit.get("text") or "").strip()
        if body and body[:200] not in shown and body not in out:
            out.append(body)
        if len(out) >= CHAT_DOC_MATCH_LIMIT:
            break
    return out


def _document_context_blocks(
    db: Session,
    settings: Settings,
    project_id: int,
    question: str,
    *,
    exclude_artifact_ids: set[int] | None = None,
) -> list[tuple[Artifact, str]]:
    """Загруженные документы, которые семантический поиск часто пропускает."""
    exclude = exclude_artifact_ids or set()
    artifacts = db.scalars(
        select(Artifact)
        .where(
            Artifact.project_id == project_id,
            Artifact.source_type == "document",
            Artifact.status == "done",
        )
        .order_by(Artifact.id.desc())
    ).all()
    ranked: list[tuple[float, int, Artifact, str]] = []
    for art in artifacts:
        if art.id in exclude:
            continue
        raw_excerpt = _load_document_raw_excerpt(settings, art)
        if not raw_excerpt:
            continue
        score = _document_relevance_score(question, art, raw_excerpt)
        min_score = 0.5 if _question_about_project_priority(question) else 1.0
        if score < min_score:
            continue
        payload = load_document_payload(settings, art)
        # дата — чтобы модель видела, что уточнение из переписки или созвона сделано позже документа
        label = material_date_label(art)
        parts = [f"Документ «{art.original_filename}»{f' · {label}' if label else ''} (artifact_id={art.artifact_id}):"]
        doc_summary = load_document_summary_md(settings, art)
        if doc_summary:
            parts.append(f"Выжимка документа:\n{doc_summary.strip()[:2500]}")
        elif payload.summary:
            parts.append(f"Выжимка: {payload.summary.strip()[:1500]}")
        if payload.participants:
            people = ", ".join(
                f"{p.name} ({p.role or 'роль не указана'})" for p in payload.participants if p.name.strip()
            )
            if people:
                parts.append(f"Участники: {people}")
        budget = CHAT_DOC_EXCERPT_WITH_SUMMARY_CHARS if (doc_summary or payload.summary) else CHAT_DOC_EXCERPT_CHARS
        matches = _document_question_matches(db, project_id, art, question, raw_excerpt[:budget])
        if matches:
            head = raw_excerpt[: min(budget, CHAT_DOC_HEAD_WITH_MATCHES_CHARS)]
            left = max(budget - len(head), 0) + CHAT_DOC_EXCERPT_WITH_SUMMARY_CHARS
            found = "\n---\n".join(matches)[:left]
            parts.append(f"Исходный текст (начало):\n{head}")
            parts.append(f"Фрагменты документа по вопросу:\n{found}")
        else:
            parts.append(f"Исходный текст:\n{raw_excerpt[:budget]}")
        ranked.append((score, art.id, art, "\n".join(parts)))
    ranked.sort(key=lambda row: (-row[0], -row[1]))
    return [(art, block) for _, _, art, block in ranked[:4]]


def _supplement_context(
    db: Session,
    settings: Settings,
    project_id: int,
    artifacts: list[Artifact],
    entities: list[Entity],
) -> str:
    """Дополнительный контекст проекта: изменения решений и противоречия (первыми, без обрезки), карточка проекта,
    пункты найденных материалов, заметки, выжимки релевантных созвонов."""
    # Только противоречия по найденным материалам, хронологией (CONFLICT-AUTONOMY-PLAN этап 5).
    from app.app.services.conflict_context import conflict_lines

    head: list[str] = []
    resolved_lines, open_lines = conflict_lines(
        db, project_id, artifact_ids={a.id for a in artifacts}, entity_ids={e.id for e in entities}
    )
    if resolved_lines:
        head.append("Изменения решений (что в силе и что заменено):\n" + "\n".join(resolved_lines))
    if open_lines:
        head.append("Нерешённые противоречия:\n" + "\n".join(open_lines))

    parts: list[str] = []

    # Карточка проекта (REQUIREMENTS-PLAN этап 10): цель, системы, этапы, участники со сторонами, термины — рамка ответа.
    from app.app.services.extraction_context import read_project_card

    card, _meta = read_project_card(settings, project_id)
    if card:
        parts.append("Карточка проекта:\n" + card[:2000])

    if entities:
        ent_lines = [f"- {format_entity_line(e.entity_type, e.value_json, e.artifact)}" for e in entities]
        parts.append("Сущности по найденным материалам:\n" + "\n".join(ent_lines))

    notes = db.scalars(
        select(UserNote).where(UserNote.project_id == project_id).order_by(UserNote.id).limit(20)
    ).all()
    if notes:
        note_lines = [f"- (note #{n.id}) {n.text.strip()}" for n in notes if n.text.strip()]
        if note_lines:
            parts.append("Заметки проекта:\n" + "\n".join(note_lines))

    summary_lines: list[str] = []
    for art in artifacts:
        if art.file_type != "audio":
            continue
        summary_path = parsed_dir(settings, art.project_id, art.source_type) / f"{art.artifact_id}_call_summary.md"
        if not summary_path.is_file():
            continue
        summary = summary_path.read_text(encoding="utf-8").strip()
        if summary:
            label = material_date_label(art)
            title = f"{art.original_filename} ({label})" if label else art.original_filename
            summary_lines.append(f"### {title}\n{summary[:2500]}")
    if summary_lines:
        parts.append("Выжимки созвонов:\n" + "\n\n".join(summary_lines))

    rest = "\n\n".join(parts)[:CHAT_SUPPLEMENT_CHARS]
    return "\n\n".join([*head, rest] if rest else head)


def _project_tz_excerpt(settings: Settings, project_id: int, *, max_chars: int = 2500) -> str:
    from app.app.services.storage import project_root

    path = project_root(settings, project_id) / "project_state" / "current_tz.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    return text[:max_chars]


def answer_chat(
    db: Session,
    settings: Settings,
    project_id: int,
    user_id: int,
    question: str,
) -> dict[str, Any]:
    from app.app.services.project_tasks import TASK_CHAT_ANSWER, track_project_task

    with track_project_task(
        db,
        task_type=TASK_CHAT_ANSWER,
        project_id=project_id,
        user_id=user_id,
    ):
        return _answer_chat_body(db, settings, project_id, user_id, question)


def _answer_chat_body(
    db: Session,
    settings: Settings,
    project_id: int,
    user_id: int,
    question: str,
) -> dict[str, Any]:
    pending_meta = _pending_clarification_for_reply(db, project_id=project_id, user_id=user_id)
    guidance_reply: str | None = None
    answer_question = question.strip()

    save_chat_message(
        db,
        project_id=project_id,
        user_id=user_id,
        role="user",
        content=question.strip(),
    )
    from app.app.services.user_activity import ACTION_QUESTION

    _log_chat_activity(
        db,
        project_id=project_id,
        user_id=user_id,
        action=ACTION_QUESTION,
        title="Вопрос в чате",
        content=question.strip(),
    )

    if pending_meta:
        guidance_reply = question.strip()
        answer_question = pending_meta.get("original_question", answer_question)
        save_chat_guidance(
            db,
            project_id=project_id,
            user_id=user_id,
            text=guidance_reply,
            topic=pending_meta.get("topic"),
        )

    pending = save_chat_message(
        db,
        project_id=project_id,
        user_id=user_id,
        role="assistant",
        content=CHAT_PENDING_TEXT,
        sources=[],
        waiting=True,
    )

    try:
        return _answer_chat_inner(
            db,
            settings,
            project_id,
            user_id,
            answer_question,
            pending,
            guidance_reply=guidance_reply,
        )
    except Exception:
        finalize_chat_message(
            db,
            pending,
            content="Не удалось получить ответ. Попробуйте ещё раз.",
            sources=[],
            waiting=False,
        )
        raise


def _answer_chat_inner(
    db: Session,
    settings: Settings,
    project_id: int,
    user_id: int,
    question: str,
    pending: ChatMessage,
    *,
    guidance_reply: str | None = None,
) -> dict[str, Any]:
    if asr_busy(db):
        answer = "Идёт транскрипция, ответ будет позже."
        finalize_chat_message(
            db,
            pending,
            content=answer,
            sources=[],
            waiting=False,
        )
        return {"answer": answer, "sources": [], "waiting": False}

    init_vector_store(get_engine())
    q_vecs = embed_texts(settings, [question])
    init_chunk_fts(get_engine())
    vec_hits = search_vectors(db, q_vecs[0], project_id=project_id, limit=CHAT_SEARCH_LIMIT * 2)
    hits = fuse_chunk_hits(
        db, question, q_vecs[0], vec_hits, project_id=project_id, limit=CHAT_SEARCH_LIMIT,
        min_score=settings.search_min_score, rerank=enrich_and_rerank_hits,
    )

    guidance_texts = list_active_guidance(db, project_id=project_id, user_id=pending.user_id)
    if guidance_reply:
        guidance_texts = [guidance_reply, *guidance_texts]

    context_lines: list[str] = []
    sources: list[dict[str, Any]] = []
    hit_artifact_db_ids: set[int] = set()
    hit_artifacts: list[Artifact] = []
    seen_artifacts: set[int] = set()
    seen_source_keys: set[tuple[str, str]] = set()

    doc_blocks = _document_context_blocks(db, settings, project_id, question)
    # Текст документов уже в контексте: фрагмент поиска из того же документа повторял бы его (пункт ТЗ шёл модели трижды)
    doc_text = {art.id: _squash(block) for art, block in doc_blocks}
    if doc_blocks:
        context_lines.append("=== Загруженные документы проекта ===")
        for art, block in doc_blocks:
            context_lines.append(block)
            key = (art.artifact_id, art.original_filename)
            if key not in seen_source_keys:
                seen_source_keys.add(key)
                sources.append(
                    {
                        "artifact_id": art.artifact_id,
                        "filename": art.original_filename,
                        "score": weighted_score(1.0, art.priority_weight),
                        "priority_weight": art.priority_weight,
                        "kind": "document",
                    }
                )
            hit_artifact_db_ids.add(art.id)
            if art.id not in seen_artifacts:
                seen_artifacts.add(art.id)
                hit_artifacts.append(art)

    for h in hits:
        art = db.get(Artifact, h["artifact_id"])
        if not art:
            continue
        # Шапка — откуда и когда фрагмент; у окон реплик имена и таймкоды уже в строках текста.
        speaker = f"[{h['speaker']}] " if h.get("speaker") and h.get("start_sec") is None else ""
        if not (art.id in doc_text and _squash(h["text"])[:DOC_DUPLICATE_PROBE] in doc_text[art.id]):
            context_lines.append(f"{chunk_header(art, h)}\n{speaker}{h['text']}")
        key = (art.artifact_id, art.original_filename)
        if key not in seen_source_keys:
            seen_source_keys.add(key)
            source = {
                "artifact_id": art.artifact_id,
                "filename": art.original_filename,
                "score": h.get("weighted_score", h["score"]),
                "priority_weight": h.get("priority_weight", art.priority_weight),
            }
            if h.get("start_sec") is not None:
                source["start_sec"] = h["start_sec"]
            # ссылка на место в стенограмме или документе (SOURCE-ANCHORS-PLAN этап 4)
            source["url"] = source_url(art.artifact_id, start_sec=h.get("start_sec"), quote=(h.get("text") or "")[:120])
            sources.append(source)
        hit_artifact_db_ids.add(art.id)
        if art.id not in seen_artifacts:
            seen_artifacts.add(art.id)
            hit_artifacts.append(art)

    entity_hits = search_entities(
        db,
        q_vecs[0],
        project_id=project_id,
        limit=ENTITY_SEARCH_LIMIT,
        min_score=settings.search_min_score,
    )
    entity_hit_ids = {h["entity_id"] for h in entity_hits}
    if entity_hits:
        context_lines.append("=== Сущности проекта по вопросу ===")
        for h in entity_hits:
            context_lines.append(f"- {h['line'][:600]} (artifact_id={h['artifact_id']})")
            art = db.get(Artifact, h["artifact_db_id"])
            if not art:
                continue
            key = (art.artifact_id, art.original_filename)
            if key not in seen_source_keys:
                seen_source_keys.add(key)
                sources.append(
                    {
                        "artifact_id": art.artifact_id,
                        "filename": art.original_filename,
                        "score": weighted_score(h["score"], art.priority_weight),
                        "priority_weight": art.priority_weight,
                        "kind": "entity",
                    }
                )
            hit_artifact_db_ids.add(art.id)
            if art.id not in seen_artifacts:
                seen_artifacts.add(art.id)
                hit_artifacts.append(art)

    from app.app.services.user_activity import build_chat_user_memory

    memory = build_chat_user_memory(db, project_id=project_id, user_id=user_id)
    tz_excerpt = _project_tz_excerpt(settings, project_id)
    try:  # журнал решений (память решений, Э6): состояние предметов, даты, стороны, цитаты; чат его только читает
        from app.app.models.project import Project
        from app.app.services import memory_mode
        from app.app.services.memory_chat import decision_block

        decisions = (decision_block(db, project_id, q_vecs[0], question)
                     if memory_mode.uses_in_chat(settings, db.get(Project, project_id)) else "")  # только в режиме live
    except Exception:  # noqa: BLE001 - память решений не должна ломать ответ чата
        logger.warning("журнал решений для чата не собран", exc_info=True)
        decisions = ""

    if not context_lines and not memory and not tz_excerpt and not decisions:
        answer = "В материалах проекта ответа нет."
        finalize_chat_message(
            db,
            pending,
            content=answer,
            sources=[],
            waiting=False,
        )
        return {"answer": answer, "sources": [], "waiting": False}

    if context_lines:
        ambiguity = detect_chat_ambiguity(
            db,
            project_id=project_id,
            question=question,
            hit_artifacts=hit_artifacts,
            guidance_texts=guidance_texts,
        )
        if ambiguity and not guidance_reply:
            answer = _build_clarification_answer(question, ambiguity)
            meta = {
                "kind": "clarification",
                "topic": ambiguity.get("reason"),
                "original_question": question,
                "ambiguity": ambiguity,
            }
            finalize_chat_message(
                db,
                pending,
                content=answer,
                sources=sources,
                waiting=False,
                meta=meta,
            )
            return {"answer": answer, "sources": sources, "waiting": False, "clarification": True}

        entities = _entities_for_artifacts(
            db, hit_artifact_db_ids, q_vecs[0], exclude=entity_hit_ids, min_score=settings.search_min_score
        )
        entity_ids = [e.id for e in entities] + list(entity_hit_ids)
        graph_part = _graph_snippet(db, project_id, entity_ids)
        supplement = _supplement_context(db, settings, project_id, hit_artifacts, entities)
        context = "\n---\n".join(context_lines)
    else:
        graph_part = ""
        supplement = ""
        context = (
            "(по этому вопросу новых фрагментов в материалах не найдено. "
            "Если вопрос — отсылка к предыдущему диалогу, ответь по нему; "
            "новые факты по проекту не выдумывай.)"
        )

    system = _load_prompt()
    user = f"Вопрос: {question}\n\n"
    if memory:
        user += memory + "\n\n"
    user += f"Контекст (найденные фрагменты по всему проекту):\n{context}"
    if guidance_texts:
        joined = "\n".join(f"- {line}" for line in guidance_texts[:8])
        user += f"\n\nУказания пользователя из чата (приоритетнее созвонов при расхождении):\n{joined}"
    if supplement:
        user += f"\n\nДополнительно из БД проекта:\n{supplement}"
    if tz_excerpt:
        user += f"\n\nАктуальная выжимка проекта (current_tz.md):\n{tz_excerpt}"
    if decisions:
        user += ("\n\nЖурнал решений проекта (предметы обсуждения: состояние, кто и когда что сказал; для вопросов «что решили», "
                 "«что отменили», «что было на дату» опирайся на него, называй даты и стороны):\n" + decisions)
    if graph_part:
        user += f"\n\n{graph_part}"
    if settings.dry_run:
        answer = f"Ответ по {len(sources)} источникам: {question}"
    else:
        # Закрыть транзакцию: ответ модели идёт десятки секунд, а соединение из пула всё это время было бы занято.
        db.commit()
        answer = chat_completion_no_think(settings, system=system, user=user, max_tokens=2048)
    from app.app.services.plain_text import plainify_llm_markup

    answer = plainify_llm_markup(answer.strip())
    finalize_chat_message(
        db,
        pending,
        content=answer,
        sources=sources,
        waiting=False,
    )
    return {"answer": answer, "sources": sources, "waiting": False}
