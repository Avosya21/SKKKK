"""Чат с ИИ по материалам проекта: история и указания пользователя, уточняющие вопросы, поиск фрагментов и сущностей, сборка контекста, ответ LLM с источниками и ожидающие сообщения (`answer_chat`)."""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.db import get_engine
from app.app.models.artifact import Artifact
from app.app.models.chat_guidance import ChatGuidance
from app.app.models.chat_message import ChatMessage
from app.app.models.entity import Chunk, Entity, Link, UserNote
from app.app.services.chat_query import claim_of, expand_query, is_pushback
from app.app.services.chat_quotes import verify_answer_quotes
from app.app.services.llm import chat_completion_no_think
from app.app.services.source_links import source_url, unit_number
from app.app.services.artifact_reports import load_document_payload
from app.app.services.summarize_document import load_document_source_text, load_document_summary_md
from app.app.services.chunk_fts import init_chunk_fts
from app.app.services.ollama_client import embed_texts
from app.app.services.storage import parsed_dir
from app.app.services.recorded_at import LOCAL_TZ, material_date, material_date_label, recorded_moment
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

logger = logging.getLogger("primaryanalitics.chat")
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
# Вопросы о времени («последний созвон», «вчера», «на встрече 09.10», «встреча-192»): поиск по смыслу дат не знает,
# и модель называла последним самый свежий из найденных созвонов (урок 2026-10-09, пр.36: встреча-192 не попала в выдачу).
CHAT_MEETING_CATALOG = 10  # созвонов в перечне «по датам» в каждом ответе
CHAT_FOCUS_MEETINGS = 3  # созвонов, которые вопрос назвал, в контекст целиком
CHAT_FOCUS_CHUNKS = 6  # фрагментов такого созвона, ближайших к вопросу
CHAT_FOCUS_FETCH = 30  # сколько ближайших смотреть, прежде чем оставить реплики: кадры формы часто ближе речи
CHAT_FOCUS_SCREEN_MAX = 1  # чужих кадров, если реплик меньше, чем мест
CHAT_FOCUS_SCREEN_BESIDE = 3  # кадров той же минуты, что выбранные реплики
SCREEN_BESIDE_SEC = 20  # кадр ещё про этот момент, если отстоит от реплики не дальше
CHAT_FOCUS_SUMMARY_CHARS = 6000
_SCREEN_MARK = "Экран (показывал"
# Запас под ChatML-обёртку /v1/completions и пустой <think></think> (см. llm.lmstudio_completions_raw).
CHAT_PROMPT_OVERHEAD_TOKENS = 400
CHAT_TRIM_NOTE = "\n\n[…часть контекста сокращена: не поместилась в окно модели…]"
_MEETING_WORD = r"(?:созвон|встреч|звонк|звонок|разговор|совещан|планерк)"
_LATEST_MEETING_RE = re.compile(
    r"(?<!\w)(пред)?(?:последн|прошл|предыдущ|свеж|недавн)\w*\s+(?:[\w-]+\s+){0,2}?" + _MEETING_WORD, re.IGNORECASE
)
_DATE_RE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?(?![\d])")
_RELATIVE_DAYS = {"позавчера": 2, "вчера": 1, "сегодня": 0}


def _squash(text: str) -> str:
    return " ".join((text or "").split())


# Хвост «Источники: …» в тексте ответа: модель дописывала свой список (с artifact_id) поверх списка ссылок под ответом.
_SOURCES_HEAD_RE = re.compile(r"^\s*[#*_]*\s*Источник(?:и|ов)?(?:\s+ответа)?\s*[*_]*\s*:", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-•*·–—]|\d+[.)])\s+")


def strip_sources_tail(answer: str) -> str:
    """Убирает из конца ответа раздел «Источники:», если после заголовка идут только пункты списка.
    Источники со ссылками показывает интерфейс под ответом; ссылки на источник внутри текста остаются."""
    lines = (answer or "").rstrip().split("\n")
    for i in range(len(lines) - 1, 0, -1):
        if _SOURCES_HEAD_RE.match(lines[i]):
            tail = [line for line in lines[i + 1:] if line.strip()]
            if all(_LIST_ITEM_RE.match(line) for line in tail):
                return "\n".join(lines[:i]).rstrip()
            return answer
    return answer


def _with_source_urls(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ссылка у каждого источника: у документов и сущностей её раньше не было (и в сохранённой истории тоже)."""
    for source in sources:
        if isinstance(source, dict) and not source.get("url"):
            source["url"] = source_url(source.get("artifact_id"))
    return sources


class ChatAlreadyPending(RuntimeError):
    """У пользователя в этом проекте уже есть вопрос, ожидающий ответа."""


# Проверка «уже ждёт ответа» и запись вопроса — под одним замком: иначе два одновременных запроса проходят оба.
_submit_guard = threading.Lock()


def chat_owner(project_id: int, user_id: int) -> tuple[str, int, int]:
    """Ключ очереди полосы чата (llm_lanes.chat_queue) для вопросов пользователя в проекте."""
    return ("chat", project_id, user_id)


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
        content = row.content
        if row.role == "assistant" and not row.waiting:  # старые ответы тоже без дубля источников
            content = strip_sources_tail(content)
        out.append(
            {
                "id": row.id,
                "role": row.role,
                "content": content,
                "sources": _with_source_urls(sources) if isinstance(sources, list) else [],
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
    from app.app.services.llm_lanes import chat_queue

    for row in rows:
        created = row.created_at
        if not created:
            continue
        if chat_queue.position(chat_owner(row.project_id, row.user_id)) is not None:
            continue  # вопрос ещё в очереди полосы чата или ответ готовится — не «завис»
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
    *,
    skip_summaries: set[int] | frozenset[int] = frozenset(),
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
        ent_lines = [f"- {chat_entity_line(e.entity_type, e.value_json, e.artifact)}" for e in entities]
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
        if art.file_type != "audio" or art.id in skip_summaries:  # выжимка уже в блоке созвона, о котором вопрос
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


def _project_meetings(db: Session, project_id: int) -> list[Artifact]:
    """Готовые созвоны проекта, новые сверху (по дате записи, иначе по дате загрузки)."""
    rows = db.scalars(
        select(Artifact).where(Artifact.project_id == project_id, Artifact.file_type == "audio", Artifact.status == "done")
    ).all()

    def key(art: Artifact) -> tuple[date, datetime]:
        day = material_date(art)[0] or date.min
        return day, art.recorded_at or art.upload_date or datetime.min

    return sorted(rows, key=key, reverse=True)


def _meeting_line(art: Artifact) -> str:
    label = material_date_label(art)
    start = recorded_moment(art)
    if start is not None:
        label += f", начало {start.astimezone(LOCAL_TZ):%H:%M}"
    if art.duration_sec:
        label += f", {round(art.duration_sec / 60)} мин"
    return f"- {art.original_filename} — {label} · artifact_id={art.artifact_id}"


def _meeting_catalog(meetings: list[Artifact]) -> str:
    if not meetings:
        return ""
    lines = [_meeting_line(a) for a in meetings[:CHAT_MEETING_CATALOG]]
    return ("Созвоны проекта по датам (новые сверху; «последний созвон» — первый в списке, "
            "«предыдущий» — второй):\n" + "\n".join(lines))


def _meetings_in_question(question: str, meetings: list[Artifact], *, today: date | None = None) -> list[Artifact]:
    """Созвоны, которые вопрос называет временем или именем: «последний/прошлый/предыдущий созвон», «вчера»,
    «09.10», «встреча-192». Поиск по смыслу их мог не найти — они идут в контекст обязательно."""
    if not meetings:
        return []
    q = question.lower()
    picked: list[Artifact] = []

    def add(art: Artifact) -> None:
        if art not in picked:
            picked.append(art)

    for m in _LATEST_MEETING_RE.finditer(q):
        add(meetings[0])
        if m.group(1) or "предыдущ" in m.group(0):  # «предпоследний», «предыдущий» — и тот, что перед последним
            if len(meetings) > 1:
                add(meetings[1])
    today = today or datetime.now(LOCAL_TZ).date()
    days: set[date] = set()
    for word, back in _RELATIVE_DAYS.items():
        if re.search(rf"(?<!\w){word}(?!\w)", q):
            days.add(today - timedelta(days=back))
    for m in _DATE_RE.finditer(q):
        dd, mm, yy = int(m.group(1)), int(m.group(2)), m.group(3)
        year = (2000 + int(yy) if len(yy) == 2 else int(yy)) if yy else None
        for art in meetings:
            day = material_date(art)[0]
            if day and day.day == dd and day.month == mm and (year is None or day.year == year):
                add(art)
    for art in meetings:
        day = material_date(art)[0]
        if day in days:
            add(art)
        stem = Path(art.original_filename or "").stem.lower()
        if len(stem) >= 5 and re.search(rf"(?<![\w-]){re.escape(stem)}(?!\w)", q):
            add(art)
    return picked[:CHAT_FOCUS_MEETINGS]


def is_screen_chunk_text(text: str) -> bool:
    """Описание кадра, а не реплика. В индексе оно отдельным окном и начинается с «Экран (показывал …)»."""
    return _SCREEN_MARK in (text or "")[:180]


def question_about_screen(question: str) -> bool:
    q = (question or "").lower().replace("ё", "е")
    return any(word in q for word in ("экран", "на экране", "показывал", "кадр"))


def _hit_span(hit: dict[str, Any]) -> tuple[float, float] | None:
    start = hit.get("start_sec")
    if start is None:
        return None
    end = hit.get("end_sec")
    return float(start), float(start if end is None else end)


def screens_beside_speech(
    speech: list[dict[str, Any]],
    screens: list[dict[str, Any]],
    *,
    slack_sec: float = SCREEN_BESIDE_SEC,
    limit: int = CHAT_FOCUS_SCREEN_BESIDE,
) -> list[dict[str, Any]]:
    """Кадр той же минуты, что реплика. Кадр из другой части созвона, даже с тем же заголовком, не берётся.

    Человек не выбирает, что важнее вообще. Он видит экран в ту секунду, когда это говорят: кадр — что видно
    сейчас, реплика — что спикер утверждает, в том числе про будущее и про «было бы».
    """
    chosen: list[dict[str, Any]] = []
    used: set[Any] = set()
    for talk in speech:
        span = _hit_span(talk)
        if span is None:
            continue
        start, end = span
        best: dict[str, Any] | None = None
        best_dist: float | None = None
        for screen in screens:
            screen_span = _hit_span(screen)
            if screen_span is None or screen.get("chunk_id") in used:
                continue
            screen_start, screen_end = screen_span
            if screen_end < start - slack_sec or screen_start > end + slack_sec:
                continue
            dist = abs(((screen_start + screen_end) / 2) - ((start + end) / 2))
            if best is None or best_dist is None or dist < best_dist:
                best, best_dist = screen, dist
        if best is None:
            continue
        used.add(best.get("chunk_id"))
        chosen.append(best)
        if len(chosen) >= limit:
            break
    return chosen


def _screen_hits(db: Session, art: Artifact) -> list[dict[str, Any]]:
    rows = db.scalars(select(Chunk).where(Chunk.artifact_id == art.id)).all()
    hits: list[dict[str, Any]] = []
    for row in rows:
        if not is_screen_chunk_text(row.text or ""):
            continue
        hits.append(
            {
                "chunk_id": row.id,
                "artifact_id": row.artifact_id,
                "chunk_index": row.chunk_index,
                "text": row.text,
                "speaker": row.speaker,
                "start_sec": row.start_sec,
                "end_sec": row.end_sec,
                "score": 0.0,
            }
        )
    return hits


def pick_focus_hits(
    hits: list[dict[str, Any]], question: str, *, limit: int = CHAT_FOCUS_CHUNKS
) -> list[dict[str, Any]]:
    """Фрагменты созвона, о котором вопрос.

    Вопрос про экран оставляет ближайшие кадры. Иначе места занимают реплики: заголовок формы на кадре
    ближе к словам «авансовый отчёт», чем разговор о том, куда девается подпись, и речь в запрос не попадает.
    Одно описание экрана остаётся, только если реплик меньше, чем мест.
    """
    ordered = list(hits)
    if question_about_screen(question):
        return ordered[:limit]
    speech = [hit for hit in ordered if not is_screen_chunk_text(hit.get("text") or "")]
    screen = [hit for hit in ordered if is_screen_chunk_text(hit.get("text") or "")]
    picked = speech[:limit]
    room = limit - len(picked)
    if room > 0 and screen:
        picked.extend(screen[: min(room, CHAT_FOCUS_SCREEN_MAX)])
    return picked


def chat_entity_line(
    entity_type: str, value_json: str, artifact: Artifact | None, *, limit: int | None = None
) -> str:
    """Строка сущности для чата. Пересказ без цитаты не выдаётся за речь: иначе модель цитирует карточку."""
    line = format_entity_line(entity_type, value_json, artifact)
    quote = ""
    try:
        value = json.loads(value_json or "{}")
    except json.JSONDecodeError:
        value = {}
    if isinstance(value, dict):
        quote = str(value.get("quote") or "").strip()
    mark = ""
    if entity_type in ("decision", "task", "open_question") and not quote:
        mark = " · пересказ, дословной цитаты нет — не приводи этот текст как речь"
    if limit is not None and len(line) + len(mark) > limit:
        line = line[: max(limit - len(mark), 0)].rstrip()
    return line + mark


def _focus_open_questions(db: Session, art: Artifact, query_vec: list[float], *, limit: int = 5) -> list[str]:
    """Открытые вопросы этого созвона, у которых есть цитата. Без цитаты это тот же пересказ, что и пустое решение."""
    rows = _entities_for_artifacts(db, {art.id}, query_vec, min_score=0.0, limit=40)
    lines: list[str] = []
    for entity in rows:
        if entity.entity_type != "open_question":
            continue
        try:
            value = json.loads(entity.value_json or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        quote = str(value.get("quote") or "").strip()
        body = str(value.get("text") or "").strip()
        if not quote or not body:
            continue
        lines.append(f"- {body} Цитата: «{quote}»")
        if len(lines) >= limit:
            break
    return lines


_SUMMARY_DROP_TITLES = ("не закрыто", "итог встречи и договорённости")


def chat_summary_for_focus(summary: str) -> str:
    """Оглавление для чата без разделов, которые уже написаны как готовый ответ.

    «Не закрыто» и «Итог» модель переносит в письмо целиком, в том числе выдуманный пункт.
    Темы, поручения и оговорки остаются: по ним видно, о чём созвон.
    """
    kept: list[str] = []
    dropping = False
    for line in summary.splitlines():
        heading = re.match(r"^#{1,3}\s+(.+?)\s*$", line)
        if heading:
            title = heading.group(1).strip().lower().rstrip(".")
            dropping = title in _SUMMARY_DROP_TITLES
            if dropping:
                continue
        if not dropping:
            kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _focus_meeting_block(
    db: Session, settings: Settings, art: Artifact, query_vec: list[float], question: str
) -> tuple[str, list[dict[str, Any]]]:
    """Блок «созвон, о котором вопрос»: оглавление без «Не закрыто» и «Итога», открытые вопросы с цитатой и реплики."""
    label = material_date_label(art)
    parts = [f"=== Созвон, о котором вопрос: {art.original_filename} ({label}) ==="]
    summary_path = parsed_dir(settings, art.project_id, art.source_type) / f"{art.artifact_id}_call_summary.md"
    if summary_path.is_file():
        summary = chat_summary_for_focus(summary_path.read_text(encoding="utf-8"))
        if summary:
            parts.append(
                "Оглавление созвона (не источник: где хранится, как передаётся и что уже решили — из реплик "
                "и кадров той же минуты ниже; пункт оглавления, которого нет там, в ответ не переноси):\n"
                + summary[:CHAT_FOCUS_SUMMARY_CHARS]
            )
    open_lines = _focus_open_questions(db, art, query_vec)
    if open_lines:
        parts.append(
            "Открытые вопросы этого созвона (ещё не выбрано; не записывай вариант как готовый механизм):\n"
            + "\n".join(open_lines)
        )
    raw_hits = search_vectors(
        db, query_vec, project_id=art.project_id, limit=CHAT_FOCUS_FETCH, artifact_id=art.id
    )
    hits = pick_focus_hits(raw_hits, question)
    if not question_about_screen(question):
        speech = [hit for hit in hits if not is_screen_chunk_text(hit.get("text") or "")]
        have = {hit.get("chunk_id") for hit in hits}
        beside = [
            hit for hit in screens_beside_speech(speech, _screen_hits(db, art)) if hit.get("chunk_id") not in have
        ]
        hits = [*hits, *beside]
        if beside:
            parts.append(
                "Кадр «Экран (показывал …)» рядом с репликой — то, что было видно в эту минуту. Реплика — что спикер "
                "утверждает, в том числе про то, чего на кадре нет: появится или было бы при настройке. Если расходятся, "
                "напиши оба: на экране видно одно, спикер говорит другое. Кадр из другой минуты к этой реплике не относится."
            )
    hits = sorted(hits, key=lambda h: (h.get("start_sec") is None, h.get("start_sec") or 0, h.get("chunk_index") or 0))
    for h in hits:
        speaker = f"[{h['speaker']}] " if h.get("speaker") and h.get("start_sec") is None else ""
        parts.append(f"{chunk_header(art, h)}\n{speaker}{h['text']}")
    return "\n---\n".join(parts), hits


def _merge_vec_hits(first: list[dict[str, Any]], second: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Находки по вопросу и по расширенному запросу: без повторов, у повтора — лучшая близость."""
    best: dict[int, dict[str, Any]] = {}
    for hit in [*first, *second]:
        cur = best.get(hit["chunk_id"])
        if cur is None or float(hit["score"]) > float(cur["score"]):
            best[hit["chunk_id"]] = hit
    return sorted(best.values(), key=lambda h: -float(h["score"]))[:limit]


def _project_materials_text(db: Session, project_id: int) -> str:
    """Полный текст готовых материалов проекта (стенограммы, документы, переписки) — для сверки цитаты, которой нет
    в контексте запроса: модель могла взять настоящую фразу из своего прошлого ответа (урок 16.10, пр. 28)."""
    from app.app.models.entity import Chunk

    parts: list[str] = []
    rows = db.execute(
        select(Artifact.id, Artifact.transcription_text).where(Artifact.project_id == project_id, Artifact.status == "done")
    ).all()
    for art_id, body in rows:
        if body and body.strip():
            parts.append(body)
        else:
            parts.extend(t for t in db.scalars(select(Chunk.text).where(Chunk.artifact_id == art_id)) if t)
    return "\n".join(parts)


def _pushback_note(question: str) -> str:
    """Указание модели, когда пользователь оспаривает прошлый ответ или утверждает факт (урок 16.10, пр. 28)."""
    return (
        "Проверка утверждения пользователя. В вопросе есть утверждение о проекте или возражение на прошлый ответ: "
        f"«{claim_of(question)}». Это гипотеза, а не факт. Проверь её по фрагментам ниже (предыдущий диалог "
        "доказательством не считается). Есть подтверждение — приведи дословную цитату с источником. Нет — так и "
        "скажи: «В материалах этого нет», и приведи ближайшее, что есть. Не соглашайся и не извиняйся только потому, "
        "что пользователь настаивает. Меняй прошлый ответ только при новой цитате и прямо назови, что в нём было неверно."
    )


def _project_tz_excerpt(settings: Settings, project_id: int, *, max_chars: int = 2500) -> str:
    from app.app.services.storage import project_root

    path = project_root(settings, project_id) / "project_state" / "current_tz.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    return text[:max_chars]


CHAT_JIRA_FOCUS_ISSUES = 2  # задач Jira, названных в вопросе по ключу, в контекст документом
CHAT_JIRA_FOCUS_CHARS = 12000  # начало документа задачи: поля, ход задачи, комментарии, трудозатраты, вложения (полная история — в конце)
_JIRA_KEY = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{1,30}-\d{1,7})(?![A-Za-z0-9_])")


_JIRA_SECTION_WORDS = (("Комментарии", ("коммент", "писал", "обсужд", "ответил")), ("Трудозатраты", ("трудозатр", "час", "списал", "времени")),
                       ("Вложения", ("файл", "вложен", "загруж", "прикреп")), ("Ход задачи", ("статус", "перев", "исполнител", "срок")),
                       ("История изменений", ("истори", "менял", "изменял")), ("Описание", ("описан", "о чём", "суть")))


def _fit_issue_document(text: str, question: str, limit_chars: int) -> str:
    """Документ задачи в пределах лимита: короткий — целиком; длинный — по разделам, каждому своя доля, а разделам,
    о которых спрашивают (комментарии, трудозатраты, вложения, ход задачи), — тройная. Так длинное описание не вытесняет остальное."""
    if len(text) <= limit_chars:
        return text
    sections = re.split(r"\n(?=## )", text)
    asked = (question or "").lower()
    weights = []
    for section in sections:
        title = section[:60]
        hot = any(title.startswith("## " + name) and any(w in asked for w in words) for name, words in _JIRA_SECTION_WORDS)
        weights.append(3 if hot else 1)
    share = limit_chars / max(sum(weights), 1)
    out = []
    for section, weight in zip(sections, weights):
        cap = int(share * weight)
        out.append(section if len(section) <= cap else section[:cap].rstrip() + "\n(раздел сокращён: остальное — в найденных фрагментах)")
    return "\n".join(out)


def jira_focus_block(db: Session, project_id: int, question: str, limit_chars: int = CHAT_JIRA_FOCUS_CHARS) -> str:
    """Вопрос называет задачу Jira по ключу («ABC-12») — её документ идёт в контекст сам, а не только найденными кусками:
    поиск по смыслу мог принести похожие разделы другой задачи (трудозатраты, комментарии у всех задач выглядят одинаково)."""
    keys = list(dict.fromkeys(m.upper() for m in _JIRA_KEY.findall(question or "")))[:8]
    if not keys:
        return ""
    try:
        from app.app.models.jira import JiraItem, JiraProjectLink

        rows = db.execute(
            select(JiraItem.ref, Artifact)
            .join(JiraProjectLink, JiraProjectLink.id == JiraItem.link_id)
            .join(Artifact, Artifact.id == JiraItem.artifact_id)
            .where(JiraProjectLink.project_id == project_id, JiraItem.kind == "issue", JiraItem.ref.in_(keys))
        ).all()
    except Exception:  # noqa: BLE001 — таблиц Jira может не быть: чат отвечает как раньше
        logger.warning("задачи Jira для чата не собраны", exc_info=True)
        return ""
    by_key = {ref: art for ref, art in rows}
    parts = []
    for key in keys:
        art = by_key.get(key)
        if art is None or len(parts) >= CHAT_JIRA_FOCUS_ISSUES:
            continue
        text = (art.transcription_text or "").strip()
        if not text:
            try:
                text = Path(art.file_path).read_text(encoding="utf-8").strip()
            except OSError:
                continue
        parts.append(f"[{art.original_filename}]\n{_fit_issue_document(text, question, limit_chars)}")
    if not parts:
        return ""
    return "Задачи Jira, названные в вопросе (документ задачи — главный источник для ответа о ней):\n" + "\n---\n".join(parts)


CHAT_JIRA_OVERVIEW_CHARS = 7000  # сводка проекта Jira в каждом ответе: счётчики и перечень задач


def jira_overview_block(db: Session, project_id: int, limit_chars: int = CHAT_JIRA_OVERVIEW_CHARS, floor: int = 1500) -> str:
    """Сводка проекта Jira (сколько задач, по статусам и исполнителям, перечень всех задач) — в каждом ответе по проекту,
    связанному с Jira. Поиск по смыслу на вопрос «сколько задач создано в Jira» приносил куски одной задачи, и помощник
    отвечал, что знает только о ней: общая картина не должна зависеть от того, что нашёл поиск."""
    try:
        from app.app.models.jira import JiraItem, JiraProjectLink

        arts = db.scalars(
            select(Artifact)
            .join(JiraItem, JiraItem.artifact_id == Artifact.id)
            .join(JiraProjectLink, JiraProjectLink.id == JiraItem.link_id)
            .where(JiraProjectLink.project_id == project_id, JiraItem.kind == "summary")
        ).all()
    except Exception:  # noqa: BLE001 — таблиц Jira может не быть: чат отвечает как раньше
        logger.warning("сводка Jira для чата не собрана", exc_info=True)
        return ""
    parts = []
    for art in arts:
        try:  # файл на диске — то, что собрано из Jira дословно; текст разбора появляется позже
            text = Path(art.file_path).read_text(encoding="utf-8").strip()
        except OSError:
            text = (art.transcription_text or "").strip()
        if not text:
            continue
        cap = max(limit_chars // max(len(arts), 1), floor)
        if len(text) > cap:
            rows = text[:cap].rsplit("\n", 1)[0]
            shown = sum(1 for line in rows.splitlines() if line.startswith("| ") and not line.startswith("| Ключ"))
            text = rows + f"\n(перечень сокращён: показано задач {shown}; остальные — в найденных фрагментах и по ключу задачи)"
        parts.append(text)
    if not parts:
        return ""
    return ("Сводка проекта в Jira — все задачи, выгруженные из Jira в этот проект (на вопросы «сколько задач/тикетов», «какие задачи», "
            "«что выгружено из Jira», «кто исполнитель» отвечай по ней; подробности задачи — в её документе «Jira КЛЮЧ-N — …»):\n"
            + "\n---\n".join(parts))


CHAT_ANSWER_TOKENS = 2048
# Нижние пределы второстепенных частей запроса: ниже них часть не сокращается, пока есть что сокращать дальше по списку.
CHAT_FLOORS = (("graph_part", 0, "граф"), ("memory", 4000, "прошлый диалог"), ("tz_excerpt", 1000, "выжимка"),
               ("supplement", 3000, "дополнение из БД"), ("decisions", 1500, "журнал решений"))
CHAT_MIN_FRAGMENTS = 6


def _keep_dialogue_tail(memory: str, cap: int) -> str:
    """Прошлый диалог сокращается с начала: заголовок и самые свежие реплики остаются."""
    if len(memory) <= cap:
        return memory
    header, _, body = memory.partition("\n")
    kept: list[str] = []
    total = len(header) + 60
    for line in reversed(body.split("\n")):
        if total + len(line) + 1 > cap:
            break
        kept.append(line)
        total += len(line) + 1
    return header + "\n(более ранние реплики опущены)\n" + "\n".join(reversed(kept))


def fit_chat_parts(room: int | None, lines: list[str], *, memory: str, supplement: str, tz_excerpt: str, decisions: str,
                   graph_part: str) -> dict[str, Any]:
    """Уложить переменные части запроса в отведённое место (знаков). Найденные фрагменты — главное, они сокращаются последними:
    сначала граф, затем старые реплики диалога, выжимка, дополнение из БД, журнал решений — каждая до своего нижнего предела;
    только если и этого мало, отбрасываются самые дальние от вопроса фрагменты (с конца списка), но не меньше CHAT_MIN_FRAGMENTS."""
    parts = {"memory": memory, "supplement": supplement, "tz_excerpt": tz_excerpt, "decisions": decisions, "graph_part": graph_part}
    kept = list(lines)
    trimmed: list[str] = []

    def need() -> int:
        return sum(len(x) + 5 for x in kept) + sum(len(v) + 120 for v in parts.values() if v)

    if room is not None:
        for name, floor, label in CHAT_FLOORS:
            excess = need() - room
            if excess <= 0:
                break
            text = parts[name]
            target = max(floor, len(text) - excess - 40)
            if target >= len(text):
                continue
            if name == "memory":
                parts[name] = _keep_dialogue_tail(text, target) if target else ""
            else:
                parts[name] = (text[:target].rstrip() + "\n(сокращено)") if target >= 200 else ""
            trimmed.append(label)
        dropped = 0
        while need() > room and len(kept) > CHAT_MIN_FRAGMENTS:
            kept.pop()
            dropped += 1
        if dropped:
            trimmed.append(f"фрагментов отброшено {dropped}")
    return {"lines": kept, "trimmed": trimmed, **parts}


def _is_context_overflow(exc: Exception) -> bool:
    text = str(exc).lower()
    return "context size" in text or "context length" in text or "n_keep" in text


def _chat_prompt_token_limit(settings: Settings, *, max_tokens: int) -> int:
    """Сколько токенов можно отдать на system+user, чтобы запрос влез в окно LM Studio."""
    from app.app.services import llm_keeper

    try:
        ctx = int(llm_keeper.load_profile().context_length)
    except Exception:  # noqa: BLE001 — профиль недоступен: берём типичное окно стенда
        ctx = 32768
    # Не больше доли чата (полосы) и не больше «окно − ответ − обёртка».
    hard = ctx - max_tokens - CHAT_PROMPT_OVERHEAD_TOKENS
    return max(4000, min(settings.lmstudio_chat_token_budget, hard))


def _fit_chat_user(settings: Settings, system: str, user: str, *, max_tokens: int = 2048) -> str:
    """Укоротить user-промпт, если он не влезает в окно модели (урок 2026-11-05: 34741 > 32768 → HTTP 400)."""
    from app.app.services.llm_lanes import CHARS_PER_TOKEN, estimate_tokens

    limit = _chat_prompt_token_limit(settings, max_tokens=max_tokens)
    if estimate_tokens(system, user) <= limit:
        return user
    body = user
    note = CHAT_TRIM_NOTE
    while estimate_tokens(system, body + note) > limit and len(body) > 2000:
        overflow = estimate_tokens(system, body + note) - limit
        cut = max(int(overflow * CHARS_PER_TOKEN) + 800, 2500)
        body = body[:-cut].rstrip()
        # Режем по границе абзаца, чтобы не рвать цитату посередине строки.
        split_at = body.rfind("\n\n")
        if split_at > len(body) // 2:
            body = body[:split_at].rstrip()
    trimmed = body + note
    logger.info(
        "chat.prompt_trimmed chars_before=%s chars_after=%s tokens_est=%s limit=%s",
        len(user),
        len(trimmed),
        estimate_tokens(system, trimmed),
        limit,
    )
    return trimmed


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

    with _submit_guard:
        if chat_is_pending(db, project_id=project_id, user_id=user_id):
            raise ChatAlreadyPending("ИИ уже готовит ответ на предыдущий вопрос")
        save_chat_message(
            db,
            project_id=project_id,
            user_id=user_id,
            role="user",
            content=question.strip(),
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

    from app.app.services.llm_lanes import chat_turn

    try:
        # Полоса чата: ответы всех пользователей и черновики MCP строго по очереди прихода (llm_lanes.chat_queue).
        with chat_turn(chat_owner(project_id, user_id), label="chat"):
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
    # Возражение («почему ты не сказал, что…») ищем по самому утверждению; запрос дополняем словами, которыми
    # о том же говорят на созвонах (урок 16.10, пр. 28: «складской контур» не находил ТСД и штрихкоды).
    pushback = is_pushback(question)
    search_text = claim_of(question) if pushback else question
    extra_words = expand_query(search_text)
    expanded = f"{search_text} {' '.join(extra_words)}".strip() if extra_words else search_text
    init_vector_store(get_engine())
    q_vecs = embed_texts(settings, [question] if expanded == question else [question, expanded])
    init_chunk_fts(get_engine())
    vec_hits = search_vectors(db, q_vecs[0], project_id=project_id, limit=CHAT_SEARCH_LIMIT * 2)
    if len(q_vecs) > 1:
        vec_hits = _merge_vec_hits(
            vec_hits, search_vectors(db, q_vecs[1], project_id=project_id, limit=CHAT_SEARCH_LIMIT * 2),
            limit=CHAT_SEARCH_LIMIT * 2,
        )
    hits = fuse_chunk_hits(
        db, expanded, q_vecs[0], vec_hits, project_id=project_id, limit=CHAT_SEARCH_LIMIT,
        min_score=settings.search_min_score, rerank=enrich_and_rerank_hits,
    )

    guidance_texts = list_active_guidance(db, project_id=project_id, user_id=pending.user_id)
    if guidance_reply:
        guidance_texts = [guidance_reply, *guidance_texts]

    context_lines: list[str] = []
    quote_parts: list[str] = []  # сверка цитат только по репликам и документам, не по оглавлению и пересказу карточек
    sources: list[dict[str, Any]] = []
    hit_artifact_db_ids: set[int] = set()
    hit_artifacts: list[Artifact] = []
    seen_artifacts: set[int] = set()
    seen_source_keys: set[tuple[str, str]] = set()

    meetings = _project_meetings(db, project_id)
    catalog = _meeting_catalog(meetings)
    focus_chunk_ids: set[int] = set()
    focus_ids: set[int] = set()
    for art in _meetings_in_question(question, meetings):
        block, focus_hits = _focus_meeting_block(db, settings, art, q_vecs[0], question)
        context_lines.append(block)
        quote_parts.extend(h["text"] for h in focus_hits if h.get("text"))
        focus_chunk_ids.update(h["chunk_id"] for h in focus_hits)
        focus_ids.add(art.id)
        key = (art.artifact_id, art.original_filename)
        seen_source_keys.add(key)
        sources.append(
            {
                "artifact_id": art.artifact_id,
                "filename": art.original_filename,
                "score": weighted_score(1.0, art.priority_weight),
                "priority_weight": art.priority_weight,
                "url": source_url(art.artifact_id),
            }
        )
        hit_artifact_db_ids.add(art.id)
        seen_artifacts.add(art.id)
        hit_artifacts.append(art)

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
        if h.get("chunk_id") in focus_chunk_ids:  # уже в блоке созвона, о котором вопрос
            pass
        elif (
            art.id in focus_ids
            and not question_about_screen(question)
            and is_screen_chunk_text(h.get("text") or "")
        ):
            continue  # остальные кадры этого созвона не вытесняют реплики, которые уже отобраны в фокус
        elif not (art.id in doc_text and _squash(h["text"])[:DOC_DUPLICATE_PROBE] in doc_text[art.id]):
            context_lines.append(f"{chunk_header(art, h)}\n{speaker}{h['text']}")
            if h.get("text"):
                quote_parts.append(h["text"])
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
            # ссылка на место в стенограмме, переписке (первое сообщение фрагмента) или документе (SOURCE-ANCHORS-PLAN этап 4)
            message_id = unit_number(h.get("unit_first"), "M")
            if message_id is not None:
                source["message_id"] = message_id
            source["url"] = source_url(art.artifact_id, start_sec=h.get("start_sec"), message_id=message_id,
                                       quote=None if message_id is not None else (h.get("text") or "")[:120])
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
            art = db.get(Artifact, h["artifact_db_id"])
            context_lines.append(
                f"- {chat_entity_line(h['entity_type'], h['value_json'], art, limit=600)} (artifact_id={h['artifact_id']})"
            )
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

    memory = build_chat_user_memory(db, project_id=project_id, user_id=user_id, question=question)
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

    jira_overview = jira_overview_block(db, project_id)
    if not context_lines and not memory and not tz_excerpt and not decisions and not catalog and not jira_overview:
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
                sources=_with_source_urls(sources),
                waiting=False,
                meta=meta,
            )
            return {"answer": answer, "sources": sources, "waiting": False, "clarification": True}

        entities = _entities_for_artifacts(
            db, hit_artifact_db_ids, q_vecs[0], exclude=entity_hit_ids, min_score=settings.search_min_score
        )
        entity_ids = [e.id for e in entities] + list(entity_hit_ids)
        graph_part = _graph_snippet(db, project_id, entity_ids)
        supplement = _supplement_context(db, settings, project_id, hit_artifacts, entities, skip_summaries=focus_ids)
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
    jira_focus = jira_focus_block(db, project_id, question)
    from app.app.services.llm_lanes import CHARS_PER_TOKEN

    # Сначала ужимаем второстепенное и сохраняем найденные фрагменты; _fit_chat_user остаётся последней страховкой окна.
    token_limit = _chat_prompt_token_limit(settings, max_tokens=CHAT_ANSWER_TOKENS)
    fixed_chars = (len(system) + len(question) + len(catalog or "") + len(jira_overview) + len(jira_focus)
                   + sum(len(g) for g in guidance_texts[:8]) + 800)
    fitted = fit_chat_parts(int(token_limit * CHARS_PER_TOKEN) - fixed_chars, context_lines,
                            memory=memory or "", supplement=supplement or "", tz_excerpt=tz_excerpt or "",
                            decisions=decisions or "", graph_part=graph_part or "")
    full_lines = context_lines
    context_lines, memory, supplement = fitted["lines"], fitted["memory"], fitted["supplement"]
    tz_excerpt, decisions, graph_part = fitted["tz_excerpt"], fitted["decisions"], fitted["graph_part"]

    def compose(level: int) -> str:
        """Запрос к модели. level 0 — всё, что уложено в окно; 1 и 2 — запасные сокращения, если модель всё же ответила «не помещается»."""
        lines = context_lines if level == 0 else context_lines[: max(len(context_lines) // 2, 4) if level == 1 else 4]
        text = f"Вопрос: {question}\n\n"
        if pushback:
            text += _pushback_note(question) + "\n\n"
        if memory and level < 2:
            text += memory + "\n\n"
        if catalog:
            text += catalog + "\n\n"
        if jira_overview:
            text += (jira_overview if level < 2 else jira_overview[: CHAT_JIRA_OVERVIEW_CHARS // 2]) + "\n\n"
        if jira_focus:
            text += (jira_focus if level == 0 else jira_focus[: CHAT_JIRA_FOCUS_CHARS // 2 * (3 - level)]) + "\n\n"
        text += "Контекст (найденные фрагменты по всему проекту):\n" + ("\n---\n".join(lines) if context_lines else context)
        if guidance_texts:
            joined = "\n".join(f"- {line}" for line in guidance_texts[:8])
            text += f"\n\nУказания пользователя из чата (приоритетнее созвонов при расхождении):\n{joined}"
        if supplement and level < 2:
            text += f"\n\nДополнительно из БД проекта:\n{supplement if level == 0 else supplement[: CHAT_SUPPLEMENT_CHARS // 2]}"
        if tz_excerpt and level == 0:
            text += f"\n\nАктуальная выжимка проекта (current_tz.md):\n{tz_excerpt}"
        if decisions and level < 2:
            text += ("\n\nЖурнал решений проекта (предметы обсуждения: состояние, кто и когда что сказал; для вопросов «что решили», "
                     "«что отменили», «что было на дату» опирайся на него, называй даты и стороны):\n" + decisions)
        if graph_part and level == 0:
            text += f"\n\n{graph_part}"
        return text

    if settings.dry_run:
        answer = f"Ответ по {len(sources)} источникам: {question}"
    else:
        # Закрыть транзакцию: ответ модели идёт десятки секунд, а соединение из пула всё это время было бы занято.
        db.commit()
        answer = ""
        user = _fit_chat_user(settings, system, compose(0), max_tokens=CHAT_ANSWER_TOKENS)
        if fitted["trimmed"]:
            logger.info("chat.prompt: сокращено: %s; фрагментов %s из %s", ", ".join(fitted["trimmed"]), len(context_lines), len(full_lines))
        for level in range(0, 3):
            if level:
                user = compose(level)
            try:
                answer = chat_completion_no_think(settings, system=system, user=user, max_tokens=CHAT_ANSWER_TOKENS)
                break
            except RuntimeError as exc:
                # Запрос не поместился в окно модели: сокращаем менее важные части и повторяем, а не отказываем человеку.
                if not _is_context_overflow(exc) or level == 2:
                    raise
                logger.warning("chat: запрос не поместился в окно модели (%s знаков, уровень %s) — сокращаю и повторяю", len(user), level)
    from app.app.services.plain_text import plainify_llm_markup

    answer = strip_sources_tail(plainify_llm_markup(answer.strip()))
    # Цитаты сверяются только с материалами: предыдущий диалог и указания пользователя доказательством не считаются.
    # Оглавление, карточки и выжимка проекта в запрос входят, но цитату не подтверждают: пересказ оттуда
    # находился бы «в материалах», хотя в репликах его нет. Нет в показанных фрагментах — ищем в тексте материалов.
    evidence = "\n".join(quote_parts)
    answer, quote_check = verify_answer_quotes(
        answer, evidence, user_texts=[question, *guidance_texts],
        source_texts=lambda: _project_materials_text(db, project_id),
    )
    if quote_check["unverified"]:
        logger.info("chat: %s из %s цитат не найдены в материалах (проект %s)",
                    len(quote_check["unverified"]), quote_check["checked"], project_id)
    _with_source_urls(sources)
    finalize_chat_message(
        db,
        pending,
        content=answer,
        sources=sources,
        waiting=False,
        meta={"quote_check": quote_check, "pushback": pushback} if quote_check["checked"] or pushback else None,
    )
    return {"answer": answer, "sources": sources, "waiting": False, "quote_check": quote_check}
