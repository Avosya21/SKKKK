"""Автопроверка требований (decision / task) локальной моделью по источнику — этап 2 docs/plans/AUTO-REVIEW-PLAN.md.

Для каждой сущности берётся источник: у созвона — окно реплик канона вокруг ``start_sec`` (или найденной по тексту
реплики), у документа — лучший по совпадению абзац. Нет источника — ``unsure`` без модели. Дубли внутри материала
отсекаются без модели. Остальное пачками уходит в LLM (промпт ``review_requirement.md``); вердикт проходит пороги
уверенности, «кто утвердил» проверяется по репликам теми же правилами, что при извлечении.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.services.auto_review import record_model_review

logger = logging.getLogger("primaryanalitics.review")

ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "review_requirement.md"
PROMPT_VERSION = "review_requirement-v2"
REQ_TYPES = ("decision", "task")
CONFIRM_MIN = 0.7  # ниже — «не уверен», решает человек
REJECT_MIN = 0.8  # отклонять модель может только увереннее, чем подтверждать
WINDOW_TURNS = 4  # реплик речи до и после реплики-источника
DUPLICATE_OVERLAP = 0.8
DOC_PARA_MIN_COVERAGE = 0.4
DOC_SOURCE_CHARS = 1500
LLM_MAX_TOKENS = 2048


class ReviewItem(BaseModel):
    id: str = ""
    verdict: str = "unsure"
    reject_kind: str | None = None
    confidence: float = 0.0
    reason: str = ""
    decided_by: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("verdict", mode="before")
    @classmethod
    def _verdict(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        return text if text in ("confirm", "reject", "unsure") else "unsure"

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("reject_kind", mode="before")
    @classmethod
    def _kind(cls, value: Any) -> str | None:
        text = str(value or "").strip().lower()
        return text if text in ("not_said", "not_decision", "duplicate", "noise") else None

    @field_validator("decided_by", mode="before")
    @classmethod
    def _decided(cls, value: Any) -> list[Any]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]


class ReviewReply(BaseModel):
    items: list[ReviewItem] = Field(default_factory=list)


@dataclass
class Candidate:
    entity_id: int
    entity_type: str
    text: str
    has_decided_by: bool
    source: list[str] = field(default_factory=list)
    anchor: Any = None  # Turn — реплика-источник созвона


def _statement(entity_type: str, value: dict[str, Any]) -> str:
    body = str(value.get("text") or value.get("description") or "").strip()
    if not body and "key" in value:  # факт: «ключ: значение»
        body = f"{value.get('key')}: {value.get('value')}"
    if entity_type == "task":
        extra = []
        if value.get("assignee"):
            extra.append(f"исполнитель: {value['assignee']}")
        if value.get("deadline"):
            extra.append(f"срок: {value['deadline']}")
        if extra:
            body = f"{body} ({'; '.join(extra)})"
    return body


def _call_source(value: dict[str, Any], text: str, turns: list) -> tuple[list[str], Any]:
    from app.app.services.entities import _match_turn
    from app.app.services.transcript_turns import format_timecode, turn_at

    speech = [t for t in turns if t.is_speech and t.start_sec is not None]
    if not speech:
        return [], None
    anchor = turn_at(turns, value["start_sec"]) if value.get("start_sec") is not None else _match_turn(text, turns, [])
    if anchor is None:
        return [], None
    pos = next((i for i, t in enumerate(speech) if t.index == anchor.index), None)
    if pos is None:
        return [], None
    window = speech[max(0, pos - WINDOW_TURNS) : pos + WINDOW_TURNS + 1]
    lines = [f"T{t.index} [{format_timecode(t.start_sec)}] {t.speaker or '?'}: {t.text}" for t in window]
    return lines, anchor


NO_SOURCE_REASON = "не найден источник (реплика/абзац) для проверки"
DOC_PARA_LOOSE_COVERAGE = 0.25  # самодостаточная формулировка (А8) добавляет объект и систему — слова абзаца совпадают хуже
DOC_ANSWER_PARAS = 2  # после абзаца-вопроса — ещё столько абзацев: строка с именем отвечающего и сам ответ


def _document_source(text: str, doc_text: str) -> list[str]:
    from app.app.services.contract_text import coverage, stems

    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", doc_text or "") if p.strip()]
    need = stems(text)
    if not paras or len(need) < 2:
        return []
    scored = [(coverage(need, stems(p)), i) for i, p in enumerate(paras)]
    best, idx = max(scored)
    if best < DOC_PARA_LOOSE_COVERAGE:
        return []
    width = 1 if best >= DOC_PARA_MIN_COVERAGE else 2  # слабое совпадение — шире окно, модель сама сверит пункт с текстом
    # Лучший абзац — вопрос: ответ ниже (в тексте переписки ещё и через строку с именем) — без него проверка видит
    # только вопрос и выключает верный пункт как «не решение» (урок 2026-09-28)
    after = width + DOC_ANSWER_PARAS if paras[idx].rstrip(" \"»')").endswith("?") else width
    around = " ".join(paras[max(0, idx - width) : idx + after + 1])
    return [around[:DOC_SOURCE_CHARS]]


def _chat_source(value: dict[str, Any], text: str, messages: list[dict]) -> list[str]:
    """Переписка: сообщение пункта (привязка ``message_id`` или лучшее совпадение слов) и соседние — как реплики созвона."""
    from app.app.services.entities import _message_line, bind_chat_source

    if not messages:
        return []
    from app.app.services.contract_text import coverage, stems

    position = {m["id"]: i for i, m in enumerate(messages)}
    mid = value.get("message_id")
    if mid not in position:
        mid = (bind_chat_source({"text": text}, messages, set()) or {}).get("message_id")
    if mid in position:
        pos = position[mid]
        return [_message_line(m) for m in messages[max(0, pos - WINDOW_TURNS): pos + WINDOW_TURNS + 1]]
    # Привязки нет: два лучших места переписки с соседними сообщениями — модель сама сверит пункт с ними
    need = stems(text)
    ranked = sorted(((coverage(need, stems(m.get("text") or "")), i) for i, m in enumerate(messages)), reverse=True)[:2]
    windows: list[str] = []
    for score, pos in sorted(ranked, key=lambda r: r[1]):
        if score < DOC_PARA_LOOSE_COVERAGE:
            continue
        for m in messages[max(0, pos - 2): pos + 3]:
            line = _message_line(m)
            if line not in windows:
                windows.append(line)
    return windows


def _duplicate_of(cands: list[Candidate]) -> dict[int, int]:
    """Дубли внутри материала без модели: у пары похожих формулировок одного типа отбрасывается короткая."""
    from app.app.services.entities import _text_overlap

    dup: dict[int, int] = {}
    for i, a in enumerate(cands):
        for b in cands[i + 1 :]:
            if a.entity_type != b.entity_type or a.entity_id in dup or b.entity_id in dup:
                continue
            if _text_overlap(a.text, b.text, min_ratio=DUPLICATE_OVERLAP):
                short, keep = (a, b) if len(a.text) < len(b.text) else (b, a)
                dup[short.entity_id] = keep.entity_id
    return dup


def _model_label(settings: Settings) -> str:
    from app.app.services.llm import provider_uses_lmstudio

    return settings.lmstudio_model if provider_uses_lmstudio(settings) else f"cursor:{settings.cursor_model}"


def _build_user(batch: list[tuple[str, Candidate]], material: str) -> str:
    parts = [f"Материал: {material}\n"]
    for key, cand in batch:
        kind = "decision" if cand.entity_type == "decision" else "task"
        parts.append(f"{key} [{kind}] {cand.text}\nИсточник:\n" + "\n".join(cand.source))
    return "\n\n".join(parts)


def _ask(settings: Settings, system: str, user: str) -> ReviewReply:
    from app.app.services.llm import chat_completion_no_think, strip_json_fence

    last_err = ""
    prompt = user
    for attempt in range(2):
        reply = chat_completion_no_think(settings, system=system, user=prompt, max_tokens=LLM_MAX_TOKENS)
        try:
            return ReviewReply.model_validate(json.loads(strip_json_fence(reply)))
        except (json.JSONDecodeError, ValidationError) as exc:
            last_err = str(exc)[:300]
            if attempt == 0:
                prompt = user + f"\n\nПредыдущий ответ невалиден: {last_err}. Верни только JSON по формату."
    raise RuntimeError(f"ответ модели не разобран: {last_err}")


def _final_verdict(item: ReviewItem) -> str:
    if item.verdict == "confirm" and item.confidence >= CONFIRM_MIN:
        return "confirm"
    if item.verdict == "reject" and item.confidence >= REJECT_MIN:
        return "reject"
    return "unsure"


def _record(db: Session, settings: Settings, entity_id: int, *, verdict: str, reason: str, confidence: float, mode: str,
            model: str, extra: dict[str, Any] | None = None, decided_by: list[dict] | None = None) -> bool:
    ent = db.get(Entity, entity_id)
    if ent is None:
        return False
    review = record_model_review(
        ent, settings, verdict=verdict, reason=reason, confidence=confidence, mode=mode, model=model,
        prompt_version=PROMPT_VERSION, extra=extra,
    )
    if review is None:
        return False
    if decided_by and mode == "live" and verdict == "confirm":
        value = json.loads(ent.value_json)
        if not value.get("decided_by"):
            value["decided_by"] = decided_by
            ent.value_json = json.dumps(value, ensure_ascii=False)
    return True


def _pending(db: Session, artifact: Artifact) -> list[Entity]:
    rows = db.scalars(
        select(Entity)
        .where(Entity.artifact_id == artifact.id, Entity.is_active.is_(True), Entity.entity_type.in_(REQ_TYPES))
        .order_by(Entity.id)
    ).all()
    out = []
    for ent in rows:
        try:
            value = json.loads(ent.value_json or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or value.get("human_review"):
            continue
        review = value.get("model_review")
        # «не найден источник» — не вердикт модели, а сбой поиска источника: проверяется заново при следующем прогоне
        retry = isinstance(review, dict) and review.get("rule") == "no_source" and not review.get("undone")
        if isinstance(review, dict) and review.get("prompt_version") == PROMPT_VERSION and not retry:
            continue
        out.append(ent)
    return out


def promote_shadow_verdicts(db: Session, settings: Settings, artifact: Artifact) -> int:
    """Режим live: применить теневые вердикты той же версии промпта без повторного вызова модели (план §7)."""
    promoted = 0
    rows = db.scalars(
        select(Entity).where(Entity.artifact_id == artifact.id, Entity.is_active.is_(True), Entity.entity_type.in_(REQ_TYPES))
    ).all()
    for ent in rows:
        try:
            value = json.loads(ent.value_json or "{}")
        except json.JSONDecodeError:
            continue
        review = value.get("model_review") if isinstance(value, dict) else None
        if not isinstance(review, dict) or review.get("mode") != "shadow" or review.get("prompt_version") != PROMPT_VERSION:
            continue
        extra = {k: v for k, v in review.items() if k not in ("verdict", "reason", "confidence", "mode", "model", "prompt_version", "at", "audit")}
        extra["promoted_from_shadow"] = review.get("at")
        if _record(db, settings, ent.id, verdict=review.get("verdict") or "unsure", reason=review.get("reason") or "",
                   confidence=float(review.get("confidence") or 0.0), mode="live", model=review.get("model") or "",
                   extra=extra, decided_by=review.get("decided_by") or None):
            promoted += 1
    return promoted


def review_artifact_requirements(db: Session, settings: Settings, artifact: Artifact, mode: str) -> dict[str, Any]:
    """Проверить требования артефакта. Вернуть счётчики: confirm / reject / unsure / без источника / дубли / вызовы LLM."""
    from app.app.services.prompts import load_prompt_block
    from app.app.services.recorded_at import material_date_label

    started = time.monotonic()
    stats = {"total": 0, "confirm": 0, "reject": 0, "unsure": 0, "no_source": 0, "duplicate": 0, "llm_calls": 0, "llm_errors": 0, "promoted": 0}
    if mode == "live":
        stats["promoted"] = promote_shadow_verdicts(db, settings, artifact)
        db.commit()
    artifact_key = artifact.artifact_id
    is_audio = artifact.file_type == "audio"
    material = f"{artifact.original_filename} ({material_date_label(artifact) or 'дата неизвестна'})"
    model = _model_label(settings)

    turns: list = []
    doc_text = ""
    chat: list[dict] = []
    if is_audio:
        from app.app.services.transcript_turns import load_turns

        turns = load_turns(settings, artifact)
    else:
        from app.app.services.chat_export import load_artifact_export
        from app.app.services.summarize_document import load_document_source_text

        chat = (load_artifact_export(settings, artifact) or {}).get("messages") or []
        doc_text = load_document_source_text(settings, artifact)

    cands: list[Candidate] = []
    for ent in _pending(db, artifact):
        value = json.loads(ent.value_json)
        text = _statement(ent.entity_type, value)
        cand = Candidate(ent.id, ent.entity_type, text, bool(value.get("decided_by")))
        if is_audio:
            cand.source, cand.anchor = _call_source(value, text, turns)
        elif chat:  # переписка: сообщение пункта и соседние (SOURCE-ANCHORS-PLAN)
            cand.source = _chat_source(value, text, chat)
        else:
            cand.source = _document_source(text, doc_text)
        cands.append(cand)
    stats["total"] = len(cands)

    for cand in [c for c in cands if not c.source]:
        _record(db, settings, cand.entity_id, verdict="unsure", reason=NO_SOURCE_REASON,
                confidence=0.0, mode=mode, model="rule", extra={"rule": "no_source"})
        stats["no_source"] += 1
        stats["unsure"] += 1
    sourced = [c for c in cands if c.source]
    dups = _duplicate_of(sourced)
    for dup_id, keep_id in dups.items():
        _record(db, settings, dup_id, verdict="reject", reason=f"дубль сущности #{keep_id} этого же материала",
                confidence=0.9, mode=mode, model="rule", extra={"rule": "duplicate", "reject_kind": "duplicate", "duplicate_of": keep_id})
        stats["duplicate"] += 1
        stats["reject"] += 1
    db.commit()

    queue = [c for c in sourced if c.entity_id not in dups]
    if not queue:
        return {**stats, "seconds": round(time.monotonic() - started, 1)}

    system = load_prompt_block(PROMPT_PATH)
    if not settings.dry_run:
        from app.app.services.gpu_phase import prepare_for_llm

        prepare_for_llm(settings.lmstudio_model, settings=settings)
    size = max(1, int(settings.auto_review_batch or 1))
    for start in range(0, len(queue), size):
        batch = [(f"E{i + 1}", c) for i, c in enumerate(queue[start : start + size])]
        user = _build_user(batch, material)
        db.commit()  # DECISIONS 2026-09-21: соединение БД не держим на время ответа LLM
        try:
            reply = _ask(settings, system, user)
        except Exception as exc:  # noqa: BLE001 — пачка останется непроверенной, следующий прогон её повторит
            stats["llm_errors"] += 1
            log_event(logger, "review.llm_failed", artifact=artifact_key, batch=len(batch), error=str(exc)[:300])
            continue
        stats["llm_calls"] += 1
        by_key = {item.id.strip().upper(): item for item in reply.items}
        for key, cand in batch:
            item = by_key.get(key)
            if item is None:
                continue  # модель пропустила пункт — повторится в следующем прогоне
            verdict = _final_verdict(item)
            decided: list[dict] = []
            if cand.entity_type == "decision" and turns and item.decided_by:
                from app.app.services.entities import _validated_decided_by

                decided = _validated_decided_by(item.decided_by, turns, cand.anchor, artifact)
            extra: dict[str, Any] = {"model_verdict": item.verdict}
            if item.reject_kind and verdict == "reject":
                extra["reject_kind"] = item.reject_kind
            if decided:
                extra["decided_by"] = decided
            if _record(db, settings, cand.entity_id, verdict=verdict, reason=item.reason, confidence=item.confidence,
                       mode=mode, model=model, extra=extra, decided_by=decided):
                stats[verdict] += 1
        db.commit()
    stats["seconds"] = round(time.monotonic() - started, 1)
    log_event(logger, "review.requirements", artifact=artifact_key, mode=mode, **stats)
    return stats
