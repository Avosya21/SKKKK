"""Извлечение сущностей (задачи, упоминания, участники, решения, факты) из нормализованного текста через LLM: разбиение, промпт, разбор JSON и фильтры (оценки длительности, последовательности этапов)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.db import get_engine
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.logging_setup import log_event
from app.app.llm_labels import llm_runtime_label
from app.app.services.graph import delete_entities_with_deps, entity_text
from app.app.services.llm import chat_completion_no_think, strip_json_fence
from app.app.services.ollama_client import embed_texts
from app.app.services.storage import structured_dir
from app.app.services.prompts import load_prompt_block
from app.app.services.vector_index import (
    delete_vectors_for_entities,
    init_vector_store,
    upsert_entity_vectors,
)

logger = logging.getLogger("skkkk.entities")

ROOT = Path(__file__).resolve().parents[3]
# v3 = v2 (контекст проекта, виды пунктов, самодостаточные формулировки — REQUIREMENTS-PLAN этап 4) + переписка по
# номерам сообщений (SOURCE-ANCHORS-PLAN этап 2); v1 и v2 оставлены файлами.
PROMPT_PATH = ROOT / "docs" / "prompts" / "extract_entities_v5.md"  # v4 = v3 + deadline_at (2026-09-30); v5 = v4 + абзацы документа P<n> (2026-10-06)
PROMPT_VERSION = "extract_entities-v5"
SOURCE_TYPE_CALL = "call"
TEXT_HASH_LEN = 16
CALL_BOUND_TYPES = {"decision", "task", "fact", "open_question"}
EXTRACT_OVERLAP_TURNS = 5  # хвост предыдущей части — только контекст, чтобы мысль не резалась на границе
DECISION_KINDS = ("requirement", "project")
TASK_KINDS = ("work", "org")
# Нечёткая формулировка (А8): начинается с местоимения или ссылается на «как обсуждали» — объект не назван.
UNCLEAR_WORDING_RE = re.compile(
    r"^\s*(это|эт[аои]|там|туда|он[аио]?|они|такж?е)\b|как\s+(обсуждали|договаривались\s+ранее)|вышеуказанн|то\s+же\s+самое",
    re.IGNORECASE,
)
# Перенос ручных решений на новые пункты при переизвлечении (А10): доля основ слов старого пункта в новом.
HUMAN_TRANSFER_MIN_COVERAGE = 0.5
HUMAN_TRANSFER_TIME_WINDOW = 45.0
REPLICA_MATCH_WINDOW = 3
TEXT_OVERLAP_MIN_RATIO = 0.35
EXTRACT_PART_CHARS = 6000
# Ответ модели на одну часть: 4096 обрезал плотные ТЗ (~12 тыс. символов JSON); окно фона 56000-22000 вмещает с запасом.
EXTRACT_MAX_TOKENS = 6144
# «Утвердил» принимается, только если реплика согласия не дальше стольких реплик от реплики решения.
DECIDED_BY_WINDOW = 5
SPEAKER_SOURCE_TRACK = "track"  # комната: имя по дорожке микрофона — доказательство
SPEAKER_SOURCE_VOICE = "voice"  # загруженная запись: имя по сопоставлению голоса — вывод


def _turn_ids(raw: Any) -> list[int]:
    """Номера реплик от LLM: 12, "12", "T12" → 12."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[int] = []
    for item in items:
        m = re.search(r"\d+", str(item))
        if m:
            out.append(int(m.group(0)))
    return out


def _msg_ids(raw: Any) -> list[int]:
    """Номера сообщений переписки от LLM: 106, "M106", "M-999698939" → со знаком (у ранних сообщений Telegram он минус)."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[int] = []
    for item in items:
        m = re.search(r"-?\d+", str(item))
        if m:
            out.append(int(m.group(0)))
    return out


def _coerce_kind(value: Any, allowed: tuple[str, ...]) -> str | None:
    kind = str(value or "").strip().lower()
    return kind if kind in allowed else None


class TaskItem(BaseModel):
    description: str = ""
    # work — работа по системе (требование), org — организационное (не требование, А7); None — старое извлечение
    kind: str | None = None
    object: str | None = None
    topic: str | None = None
    assignee: str | None = None
    deadline: str | None = None
    deadline_at: str | None = None  # срок точно: ГГГГ-ММ-ДД или ГГГГ-ММ-ДДTЧЧ:ММ (считает модель от даты созвона)
    status: str = "unknown"
    turns: list[int] = Field(default_factory=list)
    messages: list[int] = Field(default_factory=list)  # переписка: номера сообщений M<n> (SOURCE-ANCHORS-PLAN этап 2)
    quote: str = ""  # документ: дословная цитата-источник (DECISION-MEMORY-PLAN этап 2); проверяется по тексту материала
    paragraphs: list[int] = Field(default_factory=list)  # документ: номера абзацев P<n> источника (DECISION-MEMORY-PLAN, Э1)

    @field_validator("paragraphs", mode="before")
    @classmethod
    def _coerce_paragraphs(cls, value: Any) -> list[int]:
        return _turn_ids(value)

    @field_validator("deadline_at", mode="before")
    @classmethod
    def _coerce_deadline_at(cls, value: Any) -> str | None:
        from app.app.services.project_deadlines import normalize_deadline_at

        return normalize_deadline_at(value)

    @field_validator("turns", mode="before")
    @classmethod
    def _coerce_turns(cls, value: Any) -> list[int]:
        return _turn_ids(value)

    @field_validator("messages", mode="before")
    @classmethod
    def _coerce_messages(cls, value: Any) -> list[int]:
        return _msg_ids(value)

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_task_kind(cls, value: Any) -> str | None:
        return _coerce_kind(value, TASK_KINDS)


class OpenQuestionItem(BaseModel):
    """Не решено: вопрос без ответа или варианты, между которыми не выбрали (А7) — не требование, а вопрос к заказчику."""

    text: str = ""
    options: list[str] = Field(default_factory=list)
    quote: str = ""  # документ: дословная цитата-источник (DECISION-MEMORY-PLAN этап 2)
    paragraphs: list[int] = Field(default_factory=list)  # документ: номера абзацев P<n> источника (DECISION-MEMORY-PLAN, Э1)

    @field_validator("paragraphs", mode="before")
    @classmethod
    def _coerce_paragraphs(cls, value: Any) -> list[int]:
        return _turn_ids(value)
    object: str | None = None
    topic: str | None = None
    turns: list[int] = Field(default_factory=list)
    messages: list[int] = Field(default_factory=list)

    @field_validator("turns", mode="before")
    @classmethod
    def _coerce_turns(cls, value: Any) -> list[int]:
        return _turn_ids(value)

    @field_validator("messages", mode="before")
    @classmethod
    def _coerce_messages(cls, value: Any) -> list[int]:
        return _msg_ids(value)

    @field_validator("options", mode="before")
    @classmethod
    def _coerce_options(cls, value: Any) -> list[str]:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        return [str(item) for item in items if str(item).strip()]


class DecidedBy(BaseModel):
    name: str = ""
    turn: int | None = None
    message: int | None = None  # переписка: номер сообщения согласия

    @field_validator("turn", mode="before")
    @classmethod
    def _coerce_turn(cls, value: Any) -> int | None:
        ids = _turn_ids(value)
        return ids[0] if ids else None

    @field_validator("message", mode="before")
    @classmethod
    def _coerce_message(cls, value: Any) -> int | None:
        ids = _msg_ids(value)
        return ids[0] if ids else None


class DecisionItem(BaseModel):
    """Решение: текст, реплики-источник (номера T<n>) и кто утвердил/согласился — не тот, кто только спросил."""

    text: str = ""
    # requirement — как работает система, project — решение по проекту (А7); None — старое извлечение
    kind: str | None = None
    object: str | None = None
    topic: str | None = None
    turns: list[int] = Field(default_factory=list)
    messages: list[int] = Field(default_factory=list)
    quote: str = ""  # документ: дословная цитата-источник (DECISION-MEMORY-PLAN этап 2)
    paragraphs: list[int] = Field(default_factory=list)  # документ: номера абзацев P<n> источника (DECISION-MEMORY-PLAN, Э1)

    @field_validator("paragraphs", mode="before")
    @classmethod
    def _coerce_paragraphs(cls, value: Any) -> list[int]:
        return _turn_ids(value)
    decided_by: list[DecidedBy] = Field(default_factory=list)

    @field_validator("turns", mode="before")
    @classmethod
    def _coerce_turns(cls, value: Any) -> list[int]:
        return _turn_ids(value)

    @field_validator("messages", mode="before")
    @classmethod
    def _coerce_messages(cls, value: Any) -> list[int]:
        return _msg_ids(value)

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_decision_kind(cls, value: Any) -> str | None:
        return _coerce_kind(value, DECISION_KINDS)

    @field_validator("decided_by", mode="before")
    @classmethod
    def _coerce_decided_by(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        return [{"name": item} if isinstance(item, str) else item for item in items]


class MentionItem(BaseModel):
    name: str = ""
    relation: str = "mentions"


class ParticipantItem(BaseModel):
    name: str = ""
    role: str | None = None


class EntitiesPayload(BaseModel):
    artifact_id: str = ""
    source_type: str = ""
    timestamp: str | None = None
    summary: str = ""
    decisions: list[DecisionItem] = Field(default_factory=list)
    tasks: list[TaskItem] = Field(default_factory=list)
    open_questions: list[OpenQuestionItem] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    mentions: list[MentionItem] = Field(default_factory=list)
    participants: list[ParticipantItem] = Field(default_factory=list)

    @field_validator("decisions", mode="before")
    @classmethod
    def _coerce_decisions(cls, value: Any) -> list[Any]:
        """Прежний формат (строки) и старые файлы structured/*_entities.json принимаются как есть."""
        if value is None:
            return []
        return [{"text": item} if isinstance(item, str) else item for item in value]


def _load_system_prompt() -> str:
    return load_prompt_block(PROMPT_PATH)


def _split_for_extraction(text: str, max_chars: int = 6000) -> list[str]:
    """Split transcript/document text into LLM-safe chunks (Gemma ctx ~32k tokens)."""
    stripped = text.strip()
    if not stripped:
        return [""]

    paras = [p.strip() for p in re.split(r"\n{2,}", stripped) if p.strip()]
    if len(paras) <= 1:
        # Stenogram lines are usually single-newline separated.
        paras = [p.strip() for p in stripped.splitlines() if p.strip()]

    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paras:
        extra = len(p) + (1 if buf else 0)
        if buf and size + extra > max_chars:
            chunks.append("\n".join(buf))
            buf = [p]
            size = len(p)
            continue
        buf.append(p)
        size += extra
    if buf:
        chunks.append("\n".join(buf))
    return chunks or [stripped[:max_chars]]


_DEADLINE_MARKERS = re.compile(
    r"(?i)"
    r"(?:"
    r"до\s+конца\s+(?:этой\s+|текущей\s+)?недел(?:и|е)"
    r"|к\s+четвергу(?:\s*[–—-]\s*пятниц(?:е|у))?(?:\s+текущей\s+недел(?:и|е))?"
    r"|четверг(?:\s*[–—-]\s*пятниц(?:а|у|е))?(?:\s*,?\s*до\s+конца\s+недел(?:и|е))?"
    r"|до\s+\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?"
    r"|к\s+\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?"
    r"|до\s+\d{1,2}\s+[а-яё]+(?:\s+\d{4})?"
    r"|к\s+\d{1,2}\s+[а-яё]+(?:\s+\d{4})?"
    r"|20\d{2}-\d{2}-\d{2}"
    r")"
)

_DURATION_RANGE = re.compile(
    r"(?i)\d{1,2}\s*[–—\-−]\s*\d{1,2}\s*(?:нед\.?|недель|недели|мес\.?|месяц)"
)
_DURATION_BARE = re.compile(
    r"(?i)^\s*\d{1,2}\s*(?:нед\.?|недель|недели|мес\.?|месяц(?:ев|а)?)\.?\s*$"
)
_CALENDAR_DEADLINE_HINT = re.compile(
    r"(?i)("
    r"\d{1,2}[./]\d{1,2}"
    r"|20\d{2}-\d{2}-\d{2}"
    r"|четверг|пятниц|понедельник|вторник|сред[аыу]|суббот|воскресень"
    r"|сегодня|завтра"
    r"|до\s+конца"
    r"|к\s+\d"
    r")"
)


_STAGE_SEQUENCE = re.compile(
    r"(?i)(?:"
    r"перед\s+этап"
    r"|после\s+(?:приёмк\w+\s+)?этап"
    r"|на\s+этапе\b"
    r")"
)


def is_duration_estimate(raw: str | None) -> bool:
    """«4–6 нед.» / «8–12 недель» — оценка длительности этапа, не дедлайн."""
    if raw is None:
        return False
    text = raw.strip()
    if not text:
        return False
    if _CALENDAR_DEADLINE_HINT.search(text):
        return False
    return bool(_DURATION_RANGE.search(text) or _DURATION_BARE.search(text))


def is_stage_sequence_phrase(raw: str | None) -> bool:
    """«перед этапом 2» — очередность работ, не календарный срок."""
    if raw is None:
        return False
    text = raw.strip()
    if not text:
        return False
    if _CALENDAR_DEADLINE_HINT.search(text):
        return False
    return bool(_STAGE_SEQUENCE.search(text))


def is_informational_schedule(raw: str | None) -> bool:
    """Длительность этапа или очередность — не поручение с дедлайном."""
    return is_duration_estimate(raw) or is_stage_sequence_phrase(raw)


_EMPTY_DEADLINE = frozenset(
    {
        "",
        "срок не указан",
        "не указан",
        "неизвестно",
        "unknown",
        "нет",
        "n/a",
        "—",
        "-",
    }
)

_SPEAKER_LABEL = re.compile(r"^SPEAKER_\d+$", re.I)
_TOKEN_STOP = frozenset(
    "и в на с по для что это как или же не нет при из к ко от до за о об".split()
)
_PREP_VERBS = re.compile(r"(?i)(подготов|направ|сформир|состав|отправ|выдать|сделать|направить)")
_DELIVERABLE = re.compile(r"(?i)(кп|коммерческ\w*\s+предлож|предложени\w*)")
_COMPONENT = re.compile(r"(?i)(включить\s+в|добавить\s+в|в\s+кп|в\s+коммерч|без\s+демо|без\s+развертыван)")


def _normalize_deadline_value(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.casefold() in _EMPTY_DEADLINE:
        return None
    if is_informational_schedule(text):
        return None
    return text


def _token_set(text: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"[\wа-яё]+", text.casefold())
        if len(tok) >= 3 and tok not in _TOKEN_STOP
    }


def _text_overlap(a: str, b: str, *, min_ratio: float = TEXT_OVERLAP_MIN_RATIO) -> bool:
    ta, tb = _token_set(a), _token_set(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= min_ratio


def _extract_deadline_phrase(text: str) -> str | None:
    match = _DEADLINE_MARKERS.search(text)
    if not match:
        return None
    phrase = match.group(0).strip(" ,.;")
    return phrase or None


def _merge_task_into_list(tasks: list[TaskItem], incoming: TaskItem) -> list[TaskItem]:
    if not incoming.description.strip():
        return tasks
    for task in tasks:
        if _text_overlap(task.description, incoming.description):
            if not _normalize_deadline_value(task.deadline) and _normalize_deadline_value(incoming.deadline):
                task.deadline = incoming.deadline
                task.deadline_at = incoming.deadline_at
            elif not task.deadline_at and incoming.deadline_at:
                task.deadline_at = incoming.deadline_at
            if not (task.assignee or "").strip() and (incoming.assignee or "").strip():
                task.assignee = incoming.assignee
            if task.status == "unknown" and incoming.status != "unknown":
                task.status = incoming.status
            if len(incoming.description) > len(task.description):
                task.description = incoming.description
            return tasks
    tasks.append(incoming)
    return tasks


def _assignee_from_participants(payload: EntitiesPayload, assignee: str | None) -> str | None:
    if not assignee or not _SPEAKER_LABEL.match(assignee.strip()):
        return assignee
    return assignee


def normalize_entities_payload(payload: EntitiesPayload) -> EntitiesPayload:
    """Post-process LLM output: перенос сроков из decisions в tasks, слияние дублей."""
    out = payload.model_copy(deep=True)

    for task in out.tasks:
        task.assignee = _assignee_from_participants(out, task.assignee)
        task.deadline = _normalize_deadline_value(task.deadline)
        if not task.deadline:
            inline = _extract_deadline_phrase(task.description)
            if inline:
                task.deadline = inline
        if not task.deadline and task.deadline_at:
            task.deadline = task.deadline_at.replace("T", " ")

    for item in out.decisions:
        decision = item.text
        deadline = _extract_deadline_phrase(decision)
        if not deadline:
            continue
        matched = False
        for task in out.tasks:
            if _text_overlap(task.description, decision) or (
                _DELIVERABLE.search(task.description) and _DELIVERABLE.search(decision)
            ):
                if not task.deadline:
                    task.deadline = deadline
                matched = True
                break
        if not matched and re.search(
            r"(?i)(подготов|направ|сделать|уточн|провест|соглас|загруз|оцен|синхрон)",
            decision,
        ):
            out.tasks.append(
                TaskItem(
                    description=decision,
                    object=item.object,
                    topic=item.topic,
                    assignee=None,
                    deadline=deadline,
                    status="open",
                    turns=list(item.turns),
                )
            )

    deduped: list[TaskItem] = []
    for task in out.tasks:
        if not task.description.strip():
            continue
        deduped = _merge_task_into_list(deduped, task)
    out.tasks = deduped
    return out


def _normalize_deadline_key(raw: str | None) -> str:
    text = (raw or "").casefold().strip()
    text = re.sub(r"[–—−]", "-", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^к\s+", "", text)
    return text


def _deadlines_equivalent(a: str | None, b: str | None) -> bool:
    na = _normalize_deadline_key(a)
    nb = _normalize_deadline_key(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    return False


def _task_parent_score(description: str) -> int:
    text = description.casefold()
    score = len(description)
    if _PREP_VERBS.search(text):
        score += 200
    if _DELIVERABLE.search(text):
        score += 100
    if _COMPONENT.search(text):
        score -= 50
    return score


def _is_component_of_deliverable(component_desc: str, parent_desc: str) -> bool:
    c, p = component_desc.casefold(), parent_desc.casefold()
    if c == p or c in p or p in c:
        return True
    if not _DELIVERABLE.search(c) and not _COMPONENT.search(component_desc):
        return False
    if _COMPONENT.search(component_desc) and (_PREP_VERBS.search(p) or _DELIVERABLE.search(p)):
        if _DELIVERABLE.search(c) or "кп" in c or "коммерч" in c:
            return True
    if ("включ" in c or "добав" in c) and ("кп" in c or "коммерч" in c):
        return _PREP_VERBS.search(p) or "кп" in p or "коммерч" in p or "предлож" in p
    if "без демо" in c or "без развертыван" in c:
        return (_PREP_VERBS.search(p) or "кп" in p) and ("кп" in p or "коммерч" in p or "предлож" in p)
    return False


def _combine_task_descriptions(primary: str, secondary: str) -> str:
    a, b = primary.strip(), secondary.strip()
    if not b or b.casefold() in a.casefold():
        return a
    if a.casefold() in b.casefold():
        return b
    return f"{a.rstrip('.')}. {b}"


def _generic_assignees() -> set[str]:
    """Исполнитель «вообще», а не человек: команда и названия компании-исполнителя из EXECUTOR_ALIASES."""
    from app.app.config import get_settings

    aliases = {a.strip().casefold() for a in (get_settings().executor_aliases or "").split(",") if a.strip()}
    return {"команда"} | aliases


def _pick_assignee(current: str | None, incoming: str | None) -> str | None:
    cur = (current or "").strip()
    inc = (incoming or "").strip()
    if not inc:
        return cur or None
    if not cur:
        return inc
    if _SPEAKER_LABEL.match(cur) and not _SPEAKER_LABEL.match(inc):
        return inc
    if len(inc) > len(cur) and cur.casefold() in _generic_assignees():
        return inc
    return cur


def merge_related_tasks(tasks: list[TaskItem]) -> list[TaskItem]:
    """Склеить tasks с одним deadline, где одна — состав/условие другой (напр. КП + кейсы в КП)."""
    if len(tasks) < 2:
        return tasks

    merged: list[TaskItem] = []
    used: set[int] = set()

    for i, task in enumerate(tasks):
        if i in used or not task.description.strip():
            continue
        if not _normalize_deadline_value(task.deadline):
            merged.append(task.model_copy(deep=True))
            used.add(i)
            continue

        group = [task]
        used.add(i)
        for j, other in enumerate(tasks):
            if j in used or not other.description.strip():
                continue
            if not _normalize_deadline_value(other.deadline):
                continue
            if not _deadlines_equivalent(task.deadline, other.deadline):
                continue
            group.append(other)
            used.add(j)

        if len(group) == 1:
            merged.append(task.model_copy(deep=True))
            continue

        group.sort(key=lambda t: _task_parent_score(t.description), reverse=True)
        parent = group[0].model_copy(deep=True)
        for child in group[1:]:
            if _is_component_of_deliverable(child.description, parent.description) or _is_component_of_deliverable(
                child.description, group[0].description
            ):
                parent.description = _combine_task_descriptions(parent.description, child.description)
                parent.assignee = _pick_assignee(parent.assignee, child.assignee)
                if parent.status == "unknown" and child.status != "unknown":
                    parent.status = child.status
            else:
                merged.append(child.model_copy(deep=True))
        merged.append(parent)

    for i, task in enumerate(tasks):
        if i not in used and task.description.strip():
            merged.append(task.model_copy(deep=True))

    return merged


def _merge_payloads(parts: list[EntitiesPayload]) -> EntitiesPayload:
    if not parts:
        return EntitiesPayload()
    base = parts[0].model_copy(deep=True)
    seen_decisions = {d.text for d in base.decisions}
    seen_questions = {q.text for q in base.open_questions}
    merged_tasks: list[TaskItem] = list(base.tasks)
    for part in parts[1:]:
        if part.summary and len(part.summary) > len(base.summary):
            base.summary = part.summary
        for d in part.decisions:
            if d.text not in seen_decisions:
                base.decisions.append(d)
                seen_decisions.add(d.text)
        for q in part.open_questions:
            if q.text and q.text not in seen_questions:
                base.open_questions.append(q)
                seen_questions.add(q.text)
        for incoming in part.tasks:
            merged_tasks = _merge_task_into_list(merged_tasks, incoming)
        base.facts.update(part.facts)
        names = {m.name for m in base.mentions}
        for m in part.mentions:
            if m.name not in names:
                base.mentions.append(m)
                names.add(m.name)
        pnames = {p.name for p in base.participants}
        for p in part.participants:
            if p.name not in pnames:
                base.participants.append(p)
                pnames.add(p.name)
    base.tasks = merged_tasks
    return normalize_entities_payload(base)


def _material_timestamp(artifact: Artifact) -> str:
    """Дата материала для LLM (дата созвона, а не момент обработки); неизвестна — пустая строка."""
    from app.app.services.recorded_at import EXPLICIT_SOURCES, to_local

    recorded = getattr(artifact, "recorded_at", None)
    if recorded is not None and getattr(artifact, "recorded_at_source", None) in EXPLICIT_SOURCES:
        return to_local(recorded).isoformat()
    from app.app.services.project_deadlines import artifact_reference_date, reference_is_explicit

    if reference_is_explicit(artifact):
        ref = artifact_reference_date(artifact)
        return ref.isoformat() if ref else ""
    return ""


def _paragraphs_header() -> str:
    return (
        "Документ разбит на пронумерованные единицы: «P<номер> текст» (заголовок, абзац, пункт списка или строка таблицы). "
        "Для decision/task/open_question укажи в paragraphs номера единиц-источника и в quote — дословную цитату из них "
        "(без «P<номер>»).\n\n"
    )


def _numbered_unit_parts(text: str, max_chars: int = EXTRACT_PART_CHARS) -> list[str]:
    """Документ: единицы ``P<n>`` (doc_units), нарезанные на части по целым единицам. Пустой текст — []."""
    from app.app.services.doc_units import numbered_lines, split_units

    lines = numbered_lines(split_units(text))
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        if buf and size + len(line) + 1 > max_chars:
            out.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        out.append("\n".join(buf))
    return out


def _turns_header() -> str:
    return (
        "Стенограмма разбита на пронумерованные реплики: «T<номер> [таймкод] Имя: текст». "
        "Для decision/task/fact укажи номера реплик-источников в turns; для decision — decided_by "
        "(кто утвердил или явно согласился, с номером его реплики).\n\n"
    )


def _numbered_turn_parts(settings: Settings, artifact: Artifact, max_chars: int = EXTRACT_PART_CHARS) -> list[str]:
    """Созвон: реплики канона с номерами ``T<n>``, нарезанные на части по целым репликам. Не созвон — []."""
    if getattr(artifact, "source_type", "") != SOURCE_TYPE_CALL:
        return []
    from app.app.services.transcript_turns import format_timecode, load_turns

    turns = load_turns(settings, artifact)
    if not turns or all(t.start_sec is None for t in turns):
        return []
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for turn in turns:
        stamp = f"[{format_timecode(turn.start_sec)}] " if turn.start_sec is not None else ""
        who = f"{turn.speaker}: " if turn.speaker else ""
        line = f"T{turn.index} {stamp}{who}{turn.text}"
        if buf and size + len(line) + 1 > max_chars:
            out.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        out.append("\n".join(buf))
    return out


CHAT_CONTEXT_MESSAGES = 15  # сколько «чужих» (уже разобранных) сообщений показать перед своими — только контекст (Я1)


def _messages_header() -> str:
    return (
        "Переписка разбита на пронумерованные сообщения: «M<номер> [дата время] Автор (в ответ на M<номер>): текст». "
        "Для decision/task/fact/open_question укажи номера сообщений-источников в messages; для decision — decided_by "
        "(кто утвердил или явно согласился, с номером ЕГО сообщения в поле message).\n\n"
    )


def _message_line(msg: dict) -> str:
    from app.app.services.chat_export import day_only

    at = datetime.fromisoformat(msg["at"])
    reply = f" (в ответ на M{msg['reply_to']})" if msg.get("reply_to") is not None else ""
    stamp = f"{at:%d.%m.%Y}" if day_only(msg) else f"{at:%d.%m.%Y %H:%M}"  # копия чата без времени — только день
    return f"M{msg['id']} [{stamp}] {msg['author']}{reply}: {msg['text']}"


def _numbered_message_parts(settings: Settings, artifact: Artifact, max_chars: int = EXTRACT_PART_CHARS) -> list[tuple[str, str]] | None:
    """Переписка (выгрузка Telegram): части по целым сообщениям «M<n> …» (SOURCE-ANCHORS-PLAN этап 2).

    Вернуть [(контекст, часть)]; None — не переписка. Сообщения, которые уже есть в более ранней выгрузке той же
    переписки (Я1), не извлекаются: до 15 последних из них идут перед частью как контекст."""
    if getattr(artifact, "file_type", "") == "audio":
        return None
    from app.app.services.chat_export import artifact_messages_path, load_export, owned_elsewhere

    path = artifact_messages_path(settings, artifact)
    export = load_export(path)
    if not export:
        return None
    skip = owned_elsewhere(path.parent, export)
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    size = 0
    context: list[str] = []
    part_context = ""
    for msg in export["messages"]:
        line = _message_line(msg)
        if msg.get("id") in skip:
            context = (context + [line])[-CHAT_CONTEXT_MESSAGES:]
            continue
        if buf and size + len(line) + 1 > max_chars:
            out.append((part_context, "\n".join(buf)))
            buf, size, part_context = [], 0, ""
        if not buf and context:
            part_context = "\n".join(context)
        context = []
        buf.append(line)
        size += len(line) + 1
    if buf:
        out.append((part_context, "\n".join(buf)))
    return out


def _parse_entities(raw: str) -> EntitiesPayload:
    cleaned = strip_json_fence(raw)
    data = json.loads(cleaned)
    return EntitiesPayload.model_validate(data)


def extract_entities_llm(settings: Settings, artifact: Artifact, text: str, context: str = "") -> EntitiesPayload:
    """Извлечь сущности частями. ``context`` — карточка проекта, оглавление созвона, темы (extraction_context);
    к каждой части созвона добавляется хвост предыдущей части — только для понимания (REQUIREMENTS-PLAN этап 4)."""
    system = _load_system_prompt()
    log_event(
        logger,
        "entities.llm.start",
        artifact=artifact.artifact_id,
        llm_provider=settings.llm_provider,
        llm=llm_runtime_label(settings.llm_provider),
        chars=len(text),
        context_chars=len(context),
        prompt=PROMPT_VERSION,
    )
    chat = _numbered_message_parts(settings, artifact, max_chars=EXTRACT_PART_CHARS)
    prefixes: list[str] = []
    if chat is not None:  # переписка Telegram: сообщения с номерами (SOURCE-ANCHORS-PLAN этап 2)
        if not chat:
            log_event(logger, "entities.chat.all_owned_elsewhere", artifact=artifact.artifact_id)
            return EntitiesPayload(artifact_id=artifact.artifact_id, source_type=artifact.source_type)
        numbered = [c for _ctx, c in chat]
        prefixes = [ctx for ctx, _c in chat]
        header = _messages_header()
    else:
        numbered = _numbered_turn_parts(settings, artifact, max_chars=EXTRACT_PART_CHARS)
        header = _turns_header() if numbered else ""
        if not numbered and getattr(artifact, "file_type", "") != "audio":  # документ: абзацы P<n> (Э1)
            numbered = _numbered_unit_parts(text, max_chars=EXTRACT_PART_CHARS)
            header = _paragraphs_header() if numbered else ""
    pieces = numbered or _split_for_extraction(text)
    parts: list[EntitiesPayload] = []
    previous_tail = ""
    for number, chunk in enumerate(pieces):
        tail = f"## Конец предыдущей части (только контекст, не извлекать)\n{previous_tail}\n\n" if previous_tail else ""
        if number < len(prefixes) and prefixes[number]:
            tail = f"## Уже разобрано в другой выгрузке этой переписки (только контекст, не извлекать)\n{prefixes[number]}\n\n" + tail
        body = f"## Основной текст\n{header}{chunk}" if (context or tail) else f"{header}{chunk}"
        user = (
            f"artifact_id={artifact.artifact_id}\n"
            f"source_type={artifact.source_type}\n"
            f"timestamp={_material_timestamp(artifact)}\n\n"
            + (f"{context}\n\n" if context else "")
            + tail
            + body
        )
        previous_tail = "\n".join(chunk.splitlines()[-EXTRACT_OVERLAP_TURNS:]) if numbered else ""
        prefix = user[: len(user) - len(chunk)]
        parts.extend(_extract_chunk(settings, artifact, system, prefix, chunk))
    return _merge_payloads(parts)


def _extract_chunk(settings: Settings, artifact: Artifact, system: str, prefix: str, chunk: str, depth: int = 0) -> list[EntitiesPayload]:
    """Одна часть. Оборванный JSON — обычно ответ упёрся в max_tokens (плотный документ): повтор даст тот же обрыв,
    поэтому часть делится пополам по строкам (до 2 раз) и каждая половина извлекается отдельно."""
    user = prefix + chunk
    last_err = ""
    for attempt in range(2):
        try:
            reply = chat_completion_no_think(settings, system=system, user=user, max_tokens=EXTRACT_MAX_TOKENS)
            payload = _parse_entities(reply)
            payload.artifact_id = artifact.artifact_id
            payload.source_type = artifact.source_type
            return [payload]
        except (json.JSONDecodeError, ValidationError, RuntimeError) as exc:
            last_err = str(exc)
            lines = chunk.splitlines()
            if isinstance(exc, json.JSONDecodeError) and depth < 2 and len(lines) > 1:
                log_event(logger, "entities.chunk.split", artifact=artifact.artifact_id, chars=len(chunk), depth=depth + 1, error=last_err)
                half = len(lines) // 2
                return [
                    *_extract_chunk(settings, artifact, system, prefix, "\n".join(lines[:half]), depth + 1),
                    *_extract_chunk(settings, artifact, system, prefix, "\n".join(lines[half:]), depth + 1),
                ]
            if attempt == 0:
                user = user + f"\n\nПредыдущий ответ невалиден: {last_err}. Верни только JSON."
            else:
                raise RuntimeError(f"entity extraction failed: {last_err}") from exc
    raise RuntimeError(f"entity extraction failed: {last_err}")


def stub_entities(artifact: Artifact, text: str) -> EntitiesPayload:
    summary = text.strip()[:500] or "empty"
    return EntitiesPayload(
        artifact_id=artifact.artifact_id,
        source_type=artifact.source_type,
        summary=summary,
        decisions=[],
        tasks=[],
        facts={},
        mentions=[],
        participants=[],
    )


def _replica_text(row: dict) -> str:
    return str(row.get("text") or "").strip()


def _leak_covers(text: str, leaks: list[dict]) -> bool:
    needle = text.casefold().strip()
    if not needle:
        return False
    for leak in leaks:
        body = str(leak.get("text") or "").casefold().strip()
        if body and (body in needle or needle in body):
            return True
    return False


def _has_span_replicas(replicas: list[dict]) -> bool:
    return any(str(row.get("source_span_id") or "").strip() for row in replicas)


def _overlap_score(text: str, replica_text: str) -> float:
    left, right = _token_set(text), _token_set(replica_text)
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _best_window_row(text: str, group: list[dict]) -> dict:
    scored = [(_overlap_score(text, _replica_text(row)), row) for row in group]
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1] if scored else group[0]


def _match_replica(text: str, replicas: list[dict]) -> dict | None:
    needle = text.casefold().strip()
    if not needle:
        return None
    for row in replicas:
        body = _replica_text(row).casefold()
        if body and (needle in body or body in needle):
            return row
    for row in replicas:
        if _text_overlap(text, _replica_text(row)):
            return row
    for index in range(len(replicas)):
        group = replicas[index : index + REPLICA_MATCH_WINDOW]
        if len(group) < 2:
            continue
        joined = " ".join(_replica_text(row) for row in group)
        if _text_overlap(text, joined):
            return _best_window_row(text, group)
    return None


def load_meeting_proof(settings: Settings, artifact: Artifact) -> tuple[list[dict], list[dict]]:
    from app.app.services.artifact_lab import latest_run_id, resolve_run

    run_id = latest_run_id(settings, artifact)
    if not run_id:
        return [], []
    path = resolve_run(settings, artifact, run_id).resolve("proof/transcript.json")
    if not path.is_file():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], []
    return list(data.get("replicas") or []), list(data.get("leaks") or [])


def filter_call_payload(
    payload: EntitiesPayload,
    artifact: Artifact,
    replicas: list[dict],
    leaks: list[dict],
) -> EntitiesPayload:
    """Только встреча из комнаты (DECISIONS 2026-09-13): текст из leaks.json в сущности не попадает."""
    data = payload.model_copy(deep=True)
    data.decisions = [
        item for item in payload.decisions if bind_call_source({"text": item.text}, artifact, replicas, leaks)
    ]
    data.tasks = [
        item for item in payload.tasks if bind_call_source(item.model_dump(), artifact, replicas, leaks)
    ]
    data.facts = {
        key: value
        for key, value in payload.facts.items()
        if bind_call_source({"key": key, "value": value}, artifact, replicas, leaks)
    }
    return data


TURN_MATCH_MIN_COVERAGE = 0.4  # доля основ слов сущности, найденных в реплике (окне реплик)
TURN_MATCH_HINTED_COVERAGE = 0.25  # порог, если на реплику указал сам LLM (номер T<n>)
TURN_MATCH_MIN_SHARED = 2  # минимум общих основ: короткие «Да», «С» ничего не доказывают


def _turn_score(need: set[str], window: list) -> tuple[float, int]:
    from app.app.services.contract_text import coverage, stems

    hay: set[str] = set()
    for turn in window:
        hay |= stems(turn.text)
    return coverage(need, hay), len(need & hay)


def _best_in_window(need: set[str], window: list):
    """Внутри окна — реплика с наибольшим собственным совпадением (якорь «кто сказал»)."""
    return max(window, key=lambda t: (_turn_score(need, [t])[1], -t.index))


def _match_turn(text: str, turns: list, turn_ids: list[int]):
    """Реплика-источник по совпадению основ слов (реплика или окно до 3 подряд).

    Номер от LLM принимается при более мягком пороге, но тоже только при совпадении текста.
    Берётся лучшая, а не первая подходящая: иначе однобуквенная реплика «С» «содержится» в любом тексте.
    """
    from app.app.services.contract_text import stems

    need = stems(text)
    if len(need) < TURN_MATCH_MIN_SHARED:
        return None
    speech = [t for t in turns if t.is_speech and t.start_sec is not None]
    position = {t.index: pos for pos, t in enumerate(speech)}

    def windows_from(pos: int):
        for size in range(1, REPLICA_MATCH_WINDOW + 1):
            window = speech[pos : pos + size]
            if len(window) == size:
                yield window

    def pick(candidates, threshold: float):
        best = None
        for window in candidates:
            cov, shared = _turn_score(need, window)
            if cov < threshold or shared < TURN_MATCH_MIN_SHARED:
                continue
            key = (cov, -len(window))
            if best is None or key > best[0]:
                best = (key, window)
        return _best_in_window(need, best[1]) if best else None

    hinted = [w for idx in turn_ids if idx in position for w in windows_from(position[idx])]
    found = pick(hinted, TURN_MATCH_HINTED_COVERAGE) if hinted else None
    if found is not None:
        return found
    return pick((w for pos in range(len(speech)) for w in windows_from(pos)), TURN_MATCH_MIN_COVERAGE)


def _same_person(claimed: str, actual: str) -> bool:
    a, b = claimed.casefold().strip(), actual.casefold().strip()
    if not a or not b:
        return False
    return a == b or a.split()[0] == b.split()[0]


def _validated_decided_by(raw: list, turns: list, anchor, artifact: Artifact) -> list[dict]:
    """«Утвердил»: только реальный говорящий реальной реплики речи рядом с решением (DECISIONS: имя не выдумывается)."""
    by_index = {t.index: t for t in turns}
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw or []:
        data = item if isinstance(item, dict) else {"name": str(item)}
        name = str(data.get("name") or "").strip()
        ids = _turn_ids(data.get("turn"))
        turn = by_index.get(ids[0]) if ids else None
        reason = ""
        if turn is None:
            reason = "no_turn"
        elif not turn.is_speech or turn.start_sec is None:
            reason = "not_speech"
        elif turn.known_speaker is None:
            reason = "unknown_speaker"
        elif anchor is not None and abs(turn.index - anchor.index) > DECIDED_BY_WINDOW:
            reason = "too_far"
        elif not _same_person(name, turn.known_speaker):
            reason = "name_mismatch"
        if reason:
            log_event(logger, "entities.decided_by.rejected", artifact=artifact.artifact_id, name=name, turn=ids[:1], reason=reason)
            continue
        if turn.known_speaker in seen:
            continue
        seen.add(turn.known_speaker)
        out.append({"name": turn.known_speaker, "start_sec": turn.start_sec})
    return out


def bind_call_source(
    value: dict,
    artifact: Artifact,
    replicas: list[dict],
    leaks: list[dict],
    *,
    turns: list | None = None,
    recorded_at: str | None = None,
) -> dict | None:
    """Привязать decision/task/fact созвона к реплике: кто сказал, когда, (для решения) кто утвердил.

    Комната (есть реплики proof с ``source_span_id``): говорящий по дорожке, ``speaker_source=track``;
    нет совпадения — ``needs_review`` (DECISIONS 2026-09-13). Загруженная запись: реплики канона ``_final.txt``,
    ``speaker_source=voice`` (опознание по голосу, не доказательство); нет совпадения — ``source_status=unbound``.
    """
    if getattr(artifact, "source_type", "") != SOURCE_TYPE_CALL:
        return value
    text = str(value.get("text") or value.get("description") or value.get("value") or "")
    if _leak_covers(text, leaks):
        return None
    turns = turns or []
    bound = dict(value)
    turn_ids = _turn_ids(bound.pop("turns", None))
    raw_decided = bound.pop("decided_by", None)
    room = _has_span_replicas(replicas)
    if not room and not turns:
        return bound
    anchor = None
    if room:
        replica = _match_replica(text, replicas)
        if replica is None:
            bound["source_span_id"] = None
            bound["needs_review"] = True
        else:
            bound["artifact_id"] = artifact.artifact_id
            bound["source_type"] = SOURCE_TYPE_CALL
            bound["speaker"] = replica.get("speaker")
            bound["speaker_source"] = SPEAKER_SOURCE_TRACK
            bound["start_sec"] = replica.get("start")
            bound["end_sec"] = replica.get("end")
            bound["source_span_id"] = replica.get("source_span_id")
            bound["text_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:TEXT_HASH_LEN]
            if turns:
                from app.app.services.transcript_turns import turn_at

                anchor = turn_at(turns, replica.get("start"))
    else:
        anchor = _match_turn(text, turns, turn_ids)
        if anchor is None:
            bound["source_status"] = "unbound"
        else:
            bound["artifact_id"] = artifact.artifact_id
            bound["source_type"] = SOURCE_TYPE_CALL
            bound["speaker"] = anchor.known_speaker
            bound["speaker_source"] = SPEAKER_SOURCE_VOICE
            bound["start_sec"] = anchor.start_sec
            bound["end_sec"] = anchor.end_sec
            bound["text_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:TEXT_HASH_LEN]
            if anchor.known_speaker is None:
                bound["source_status"] = "speaker_unknown"
    if recorded_at and bound.get("start_sec") is not None:
        bound["recorded_at"] = recorded_at
    if turns:
        _bind_assignee_label(bound, turns, anchor)
    if raw_decided is not None and turns:
        bound["decided_by"] = _validated_decided_by(raw_decided, turns, anchor, artifact)
    return bound


def _bind_assignee_label(bound: dict, turns: list, anchor: Any) -> None:
    """Исполнитель — метка неназванного голоса (speaker_4): запомнить реплику этого голоса, чтобы после
    переименования спикера (rebind_call_entities) подставить имя. Голос уже назван — имя сразу."""
    label = str(bound.get("assignee") or "").strip()
    if not label or not _SPEAKER_LABEL.match(label):
        return
    own = [t for t in turns if (t.speaker or "").strip().casefold() == label.casefold() and t.start_sec is not None]
    if not own:
        return
    near = getattr(anchor, "start_sec", None)
    turn = min(own, key=lambda t: abs(t.start_sec - near)) if near is not None else own[0]
    bound["assignee_start_sec"] = turn.start_sec
    if turn.known_speaker:
        bound["assignee"] = turn.known_speaker


SPEAKER_SOURCE_CHAT = "chat"  # переписка: автор из разметки выгрузки — доказательство, как дорожка комнаты
CHAT_MATCH_HINTED = 0.3  # доля основ слов пункта в сообщении, названном моделью (и соседних)
CHAT_MATCH_GLOBAL = 0.5  # то же при поиске по всей переписке, если названное не подошло
CHAT_AGREE_MESSAGES = 10  # Я2: согласие — не дальше стольких сообщений после сообщения пункта…
CHAT_AGREE_HOURS = 24  # …и не позже стольких часов (или прямым ответом на него)


def _chat_score(need: set[str], msg: dict) -> float:
    from app.app.services.contract_text import coverage, stems

    return coverage(need, stems(msg.get("text") or "")) if need else 0.0


_QUESTION_END = re.compile(r"\?[\s\"»')\]]*$")


def _is_question(text: str) -> bool:
    """Сообщение — вопрос: его последняя непустая строка кончается «?»."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return bool(lines) and _QUESTION_END.search(lines[-1]) is not None


def _answer_candidates(messages: list[dict], pos: int) -> list[int]:
    """Возможные ответы другого участника на вопрос в ``pos``: следующее сообщение (если оно не ответ на что-то другое)
    и ответы на вопрос (``reply_to``) в пределах окна Я2 — какой из них ответ, говорит модель (``_chat_anchor``)."""
    question = messages[pos]
    out: list[int] = []
    if pos + 1 < len(messages):
        nxt = messages[pos + 1]
        if nxt["author"] != question["author"] and nxt.get("reply_to") in (None, question["id"]):
            out.append(pos + 1)
    for j in range(pos + 1, min(len(messages), pos + 1 + CHAT_AGREE_MESSAGES)):
        msg = messages[j]
        if msg.get("reply_to") == question["id"] and msg["author"] != question["author"] and j not in out:
            out.append(j)
    return out


def _question_pos(messages: list[dict], pos: int) -> int | None:
    """Вопрос другого участника, на который отвечает сообщение ``pos`` (ответом на него или следующим сообщением).
    Вопрос вслед за вопросом — не ответ."""
    msg = messages[pos]
    if _is_question(msg.get("text") or ""):
        return None
    if msg.get("reply_to") is not None:
        target = next((j for j in range(pos - 1, -1, -1) if messages[j]["id"] == msg["reply_to"]), None)
        if target is not None and messages[target]["author"] != msg["author"] and _is_question(messages[target].get("text") or ""):
            return target
        return None
    if pos > 0 and messages[pos - 1]["author"] != msg["author"] and _is_question(messages[pos - 1].get("text") or ""):
        return pos - 1
    return None


def _chat_anchor(messages: list[dict], pos: int, *, answer_follows: bool = True, evidence: set[int] | None = None) -> tuple[int, dict]:
    """Сообщение-источник пункта и поля привязки. Совпадение слов тянет пункт к вопросу (вопрос повторяет тему, ответ
    короткий), но решение — в ответе: если модель назвала ответ среди сообщений пункта или «утвердил» (``evidence``),
    источник — ответ, вопрос остаётся контекстом (``answer_to`` — номер вопроса, ``asked_by`` — кто спросил). Без такого
    указания пункт остаётся на вопросе: это может быть предложение, которое ответ отклонил. Урок 2026-09-28: «список
    опоздавших не отсылается» привязался к вопросу, и проверка выключила верный пункт как «вопрос, а не решение»."""
    from app.app.services.chat_export import AT_PRECISION_DAY, day_only

    question = None
    if answer_follows and evidence and _is_question(messages[pos].get("text") or ""):
        answer = next((j for j in _answer_candidates(messages, pos) if messages[j]["id"] in evidence), None)
        if answer is not None:
            question, pos = pos, answer
    if question is None:
        question = _question_pos(messages, pos)
    msg = messages[pos]
    fields = {"message_id": msg["id"], "message_at": msg["at"], "speaker": msg["author"], "speaker_source": SPEAKER_SOURCE_CHAT,
              "source_type": "chat"}
    if day_only(msg):
        fields["message_at_precision"] = AT_PRECISION_DAY
    if question is not None:
        fields["answer_to"], fields["asked_by"] = messages[question]["id"], messages[question]["author"]
    return pos, fields


CHAT_ANCHOR_KEYS = ("message_id", "message_at", "message_at_precision", "speaker", "speaker_source", "source_type", "answer_to", "asked_by")


def bind_chat_source(value: dict, messages: list[dict], skip: set[int], *, answer_follows: bool = True) -> dict | None:
    """Привязать пункт переписки к сообщению (SOURCE-ANCHORS-PLAN этап 2): номер, время, автор — из выгрузки.

    Сообщение, которое принадлежит более ранней выгрузке той же переписки (Я1), — не источник: пункт отбрасывается.
    «Утвердил» (Я2) — другой участник, ответом на сообщение пункта или следующим сообщением в пределах
    ``CHAT_AGREE_MESSAGES`` сообщений и ``CHAT_AGREE_HOURS`` часов. Лучшее совпадение — вопрос, на который ответили:
    источник — ответ (``_chat_anchor``); ``answer_follows=False`` — для открытых вопросов, их источник — сам вопрос."""
    from app.app.services.contract_text import stems

    bound = dict(value)
    ids = _msg_ids(bound.pop("messages", None)) or _msg_ids(bound.pop("turns", None))
    bound.pop("turns", None)
    raw_decided = bound.pop("decided_by", None)
    text = str(bound.get("text") or bound.get("description") or "")
    need = stems(text)
    position = {m["id"]: i for i, m in enumerate(messages)}
    hinted = {j for mid in ids if mid in position for j in range(max(0, position[mid] - 2), min(len(messages), position[mid] + 3))}
    best = None
    for pos in sorted(hinted):
        score = _chat_score(need, messages[pos])
        if score >= CHAT_MATCH_HINTED and (best is None or score > best[0]):
            best = (score, pos)
    if best is None:
        for pos, msg in enumerate(messages):
            score = _chat_score(need, msg)
            if score >= CHAT_MATCH_GLOBAL and (best is None or score > best[0]):
                best = (score, pos)
    if best is None:
        bound["source_status"] = "unbound"
        return bound
    evidence = set(ids) | {mid for item in raw_decided or [] if isinstance(item, dict)
                           for mid in (_msg_ids(item.get("message")) or _msg_ids(item.get("turn")))[:1]}
    anchor_pos, fields = _chat_anchor(messages, best[1], answer_follows=answer_follows, evidence=evidence)
    anchor = messages[anchor_pos]
    if anchor["id"] in skip:
        return None
    bound.update(fields)
    agreed: list[dict] = []
    anchor_at = datetime.fromisoformat(anchor["at"])
    for item in raw_decided or []:
        data = item if isinstance(item, dict) else {"name": str(item)}
        mid = (_msg_ids(data.get("message")) or _msg_ids(data.get("turn")) or [None])[0]
        if mid not in position:
            continue
        msg = messages[position[mid]]
        gap = position[mid] - anchor_pos
        hours = (datetime.fromisoformat(msg["at"]) - anchor_at).total_seconds() / 3600
        close = 0 < gap <= CHAT_AGREE_MESSAGES and 0 <= hours <= CHAT_AGREE_HOURS
        reply = msg.get("reply_to") == anchor["id"] and 0 <= hours <= CHAT_AGREE_HOURS
        if msg["author"] == anchor["author"] or not (close or reply) or any(a["name"] == msg["author"] for a in agreed):
            continue
        entry = {"name": msg["author"], "message_id": msg["id"], "at": msg["at"]}
        if fields.get("message_at_precision"):
            entry["at_precision"] = fields["message_at_precision"]
        agreed.append(entry)
    if agreed:
        bound["decided_by"] = agreed
    return bound


CHAT_REBIND_MIN = 0.35  # замер «ПИЛОТА»: самодостаточные формулировки совпадают с сообщением на 0.36–0.47
CHAT_REBIND_MARGIN = 0.05  # …но лучшее сообщение должно заметно обгонять второе, иначе автор и время были бы угаданы


def _best_distinct_message(value: dict, messages: list[dict]) -> dict | None:
    """Мягкая привязка без номеров сообщений от модели: лучшее сообщение, если оно заметно лучше второго."""
    from app.app.services.contract_text import stems

    need = stems(str(value.get("text") or value.get("description") or ""))
    if not need:
        return None
    ranked = sorted(((_chat_score(need, m), i) for i, m in enumerate(messages)), reverse=True)
    if not ranked or ranked[0][0] < CHAT_REBIND_MIN:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < CHAT_REBIND_MARGIN:
        return None
    return _chat_anchor(messages, ranked[0][1])[1]  # без номеров от модели — вопрос к ответу не переносится


def rebind_chat_entities(db: Session, settings: Settings, artifact: Artifact) -> dict[str, int]:
    """Привязать к сообщениям пункты переписки, извлечённые до разбора выгрузки (без модели, на месте; id и решения целы).

    Совпадение слов пункта с сообщением (порог ``CHAT_MATCH_GLOBAL``); автор и время — из выгрузки. Пункт из сообщения,
    которое принадлежит более ранней выгрузке (Я1), помечается ``owned_elsewhere`` — его источник в другой выгрузке."""
    from app.app.services.chat_export import artifact_messages_path, load_export, owned_elsewhere

    path = artifact_messages_path(settings, artifact)
    export = load_export(path)
    stats = {"bound": 0, "already": 0, "unbound": 0, "owned_elsewhere": 0}
    if not export:
        return stats
    messages = export["messages"]
    skip = owned_elsewhere(path.parent, export)
    rows = db.scalars(select(Entity).where(Entity.artifact_id == artifact.id, Entity.entity_type.in_(CALL_BOUND_TYPES))).all()
    for ent in rows:
        try:
            value = json.loads(ent.value_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        if value.get("message_id") is not None:
            stats["already"] += 1
            continue
        probe = {k: value[k] for k in ("text", "description") if value.get(k)}
        bound = bind_chat_source(probe, messages, set())
        if not bound or bound.get("message_id") is None:
            bound = _best_distinct_message(probe, messages)
        if not bound or bound.get("message_id") is None:
            stats["unbound"] += 1
            continue
        for key in CHAT_ANCHOR_KEYS:
            if key in bound:
                value[key] = bound[key]
            else:
                value.pop(key, None)
        value.pop("source_status", None)
        if bound["message_id"] in skip:
            value["owned_elsewhere"] = True
            stats["owned_elsewhere"] += 1
        ent.value_json = json.dumps(value, ensure_ascii=False)
        stats["bound"] += 1
    db.commit()
    log_event(logger, "entities.chat_rebound", artifact=artifact.artifact_id, **stats)
    return stats


_QUOTE_TRIM = " \t\r\n\"'«»“”„…-–—.,;:!?()[]{}"


def _norm_quote(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip(_QUOTE_TRIM)


def verify_document_quote(quote: str, text: str) -> str | None:
    """Цитата документа принимается, только если она дословно (без учёта регистра, пробелов, ё и кавычек) есть в тексте
    материала. Возвращает цитату как есть или None: модель не должна «цитировать» то, чего в источнике нет."""
    norm = _norm_quote(quote)
    if len(norm) < 12:
        return None
    haystack = re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е"))
    return quote.strip() if norm in haystack else None


DOC_HINT_COVERAGE = 0.3  # подсказанная моделью единица принимается без дословной цитаты, если покрывает слова пункта хотя бы так


def bind_document_source(value: dict, units: list, doc_text: str) -> dict:
    """Привязать пункт документа к абзацу-источнику (Э1): ``source_paragraph`` (номер P<n>) и ``source_quote``.

    Порядок: дословная цитата модели, найденная в тексте → единица с цитатой (подсказанная моделью — первой); нет цитаты, но
    подсказана единица, слова которой покрывают пункт, → цитату выбирает код (``source_basis=hint``); иначе пункт без
    источника (``source_status=quote_not_found``). Цитату, которой нет в тексте, не принимаем."""
    from app.app.services import contract_text as ct
    from app.app.services.doc_units import find_unit_by_quote, unit_by_id

    bound = dict(value)
    claimed = str(bound.pop("quote", "") or "")
    hints = [int(x) for x in (bound.pop("paragraphs", None) or []) if isinstance(x, int)]
    statement = str(bound.get("text") or bound.get("description") or "")
    verified = verify_document_quote(claimed, doc_text) if claimed else None
    unit = find_unit_by_quote(units, verified, prefer=hints) if verified else None
    if verified and unit is not None:
        bound["source_quote"], bound["source_paragraph"] = verified, unit.id
        return bound
    if verified:  # цитата в тексте есть, но единицу не определили (спан через несколько единиц): адрес — подсказанная
        hinted = unit_by_id(units, hints[0]) if hints else None
        bound["source_quote"] = verified
        if hinted is not None:
            bound["source_paragraph"] = hinted.id
        return bound
    for hint in hints:
        hinted = unit_by_id(units, hint)
        if hinted is None:
            continue
        need = ct.stems(statement)
        if need and ct.coverage(need, ct.stems(hinted.text)) >= DOC_HINT_COVERAGE:
            found = ct.best_quote(statement, hinted.text)
            bound["source_paragraph"] = hinted.id
            bound["source_quote"] = found[0] if found else hinted.text[: ct.QUOTE_MAX]
            bound["source_basis"] = "hint"
            return bound
    bound["source_status"] = "quote_not_found"  # модель не привела дословную цитату и подходящую единицу
    return bound


def _decision_value(item: DecisionItem) -> dict:
    value: dict[str, Any] = {
        "text": item.text,
        "turns": list(item.turns),
        "decided_by": [d.model_dump() for d in item.decided_by],
    }
    for key in ("kind", "object", "topic"):
        if getattr(item, key):
            value[key] = getattr(item, key)
    if item.messages:
        value["messages"] = list(item.messages)
    if item.quote:
        value["quote"] = item.quote
    if item.paragraphs:
        value["paragraphs"] = list(item.paragraphs)
    return value


def unclear_wording(text: str) -> bool:
    """Формулировка без объекта (А8): «Это сделать…», «как обсуждали…» — в очереди будет пометка «нечёткая формулировка»."""
    return bool(UNCLEAR_WORDING_RE.search(text or ""))


def _clean_value(value: dict) -> dict:
    """Пустые kind/object/topic не храним (старые пункты их не имеют); нечёткую формулировку помечаем."""
    out = {k: v for k, v in value.items() if not (k in ("kind", "object", "topic") and not v)}
    text = str(out.get("text") or out.get("description") or "")
    if text and unclear_wording(text):
        out["unclear_wording"] = True
    return out


HUMAN_TYPES = ("decision", "task", "open_question")


def _human_snapshot(db: Session, artifact: Artifact) -> list[dict]:
    """Ручные решения по пунктам артефакта до переизвлечения (А10): подтверждение, статус, отказ, проигрыш в конфликте."""
    from app.app.models.entity import Resolution

    rows = db.scalars(select(Entity).where(Entity.artifact_id == artifact.id, Entity.entity_type.in_(HUMAN_TYPES))).all()
    lost = set(db.scalars(select(Resolution.losing_entity_id).where(Resolution.resolved_by.is_not(None))).all()) if rows else set()
    out: list[dict] = []
    for ent in rows:
        try:
            value = json.loads(ent.value_json or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        human = value.get("human_review")
        if not human and ent.id not in lost:
            continue
        if not human:
            human = {"action": "reject", "by": "решение по конфликту", "reason": "проиграл в конфликте, решённом человеком"}
        out.append({"id": ent.id, "type": ent.entity_type, "text": str(value.get("text") or value.get("description") or ""),
                    "start_sec": value.get("start_sec"), "human_review": human, "human_status": value.get("human_status"),
                    "is_active": ent.is_active and ent.id not in lost})
    return out


def _transfer_human(rows: list[Entity], snapshot: list[dict]) -> tuple[int, list[dict]]:
    """Перенести ручные решения на новые пункты с той же мыслью (основы слов) и рядом по времени. Вернуть (перенесено, не нашлось)."""
    from app.app.services.contract_text import coverage, stems

    if not snapshot:
        return 0, []
    news = []
    for ent in rows:
        if ent.entity_type not in HUMAN_TYPES:
            continue
        value = json.loads(ent.value_json)
        news.append((ent, value, stems(str(value.get("text") or value.get("description") or ""))))
    taken: set[int] = set()
    moved, missing = 0, []
    for snap in snapshot:
        need = stems(snap["text"])
        best = None
        for ent, value, have in news:
            if ent.id in taken or not need:
                continue
            score = coverage(need, have)
            if snap.get("start_sec") is not None and value.get("start_sec") is not None:
                if abs(float(snap["start_sec"]) - float(value["start_sec"])) <= HUMAN_TRANSFER_TIME_WINDOW:
                    score += 0.15
            if best is None or score > best[0]:
                best = (score, ent, value)
        if best is None or best[0] < HUMAN_TRANSFER_MIN_COVERAGE:
            missing.append({"old_entity_id": snap["id"], "type": snap["type"], "text": snap["text"], "human_review": snap["human_review"]})
            continue
        _score, ent, value = best
        taken.add(ent.id)
        value["human_review"] = {**snap["human_review"], "transferred_from": snap["id"]}
        if snap.get("human_status"):
            value["human_status"] = snap["human_status"]
        ent.value_json = json.dumps(value, ensure_ascii=False)
        ent.needs_review = False
        ent.is_active = bool(snap["is_active"])
        moved += 1
    return moved, missing


def _write_transfer_report(settings: Settings, artifact: Artifact, moved: int, missing: list[dict]) -> None:
    from app.app.services.storage import project_root

    path = project_root(settings, artifact.project_id) / "project_state" / "reextract_human_transfer.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data[artifact.artifact_id] = {"file": artifact.original_filename, "transferred": moved, "not_found": missing,
                                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def rebind_call_entities(db: Session, settings: Settings, artifact: Artifact) -> int:
    """Обновить имена говорящих у привязанных по голосу сущностей после переименования спикеров. Без LLM, на месте.

    Строки не пересоздаются (id, связи графа, разрешения, human_review целы). Якорь — ``start_sec``: номера реплик
    сдвигаются после «Слить». Сущности без привязки (старые созвоны) и привязка по дорожке (комната) не трогаются.
    """
    if getattr(artifact, "source_type", "") != SOURCE_TYPE_CALL or artifact.file_type != "audio":
        return 0
    from app.app.services.transcript_turns import load_turns, turn_at

    turns = load_turns(settings, artifact)
    if not turns:
        return 0
    rows = db.scalars(
        select(Entity).where(Entity.artifact_id == artifact.id, Entity.entity_type.in_(CALL_BOUND_TYPES))
    ).all()
    changed = 0
    for ent in rows:
        try:
            value = json.loads(ent.value_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        before = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if value.get("assignee_start_sec") is not None:
            owner = turn_at(turns, value["assignee_start_sec"])
            if owner is not None and owner.known_speaker:
                value["assignee"] = owner.known_speaker
        if value.get("speaker_source") != SPEAKER_SOURCE_VOICE:
            if json.dumps(value, ensure_ascii=False, sort_keys=True) != before:
                ent.value_json = json.dumps(value, ensure_ascii=False)
                changed += 1
            continue
        turn = turn_at(turns, value.get("start_sec"))
        if turn is not None:
            value["speaker"] = turn.known_speaker
            if turn.known_speaker is None:
                value["source_status"] = "speaker_unknown"
            elif value.get("source_status") == "speaker_unknown":
                value.pop("source_status", None)
        for item in value.get("decided_by") or []:
            if isinstance(item, dict):
                other = turn_at(turns, item.get("start_sec"))
                if other is not None and other.known_speaker:
                    item["name"] = other.known_speaker
        if json.dumps(value, ensure_ascii=False, sort_keys=True) != before:
            ent.value_json = json.dumps(value, ensure_ascii=False)
            changed += 1
    if changed:
        db.flush()
        log_event(logger, "entities.rebound", artifact=artifact.artifact_id, changed=changed)
    return changed


def persist_entities(
    db: Session,
    settings: Settings,
    artifact: Artifact,
    payload: EntitiesPayload,
) -> Path:
    payload.tasks = merge_related_tasks(payload.tasks)
    replicas, leaks = ([], [])
    turns: list = []
    recorded_at = None
    if getattr(artifact, "source_type", "") == SOURCE_TYPE_CALL:
        from app.app.services.transcript_turns import load_turns

        replicas, leaks = load_meeting_proof(settings, artifact)
        turns = load_turns(settings, artifact)
        stamp = _material_timestamp(artifact)
        recorded_at = stamp or None
        if _has_span_replicas(replicas):
            payload = filter_call_payload(payload, artifact, replicas, leaks)
    chat_messages: list[dict] = []
    chat_skip: set[int] = set()
    if getattr(artifact, "file_type", "") != "audio":
        from app.app.services.chat_export import artifact_messages_path, load_export, owned_elsewhere

        chat_path = artifact_messages_path(settings, artifact)
        export = load_export(chat_path)
        if export:
            chat_messages = export["messages"]
            chat_skip = owned_elsewhere(chat_path.parent, export)
    doc_text = ""
    if getattr(artifact, "file_type", "") != "audio" and not chat_messages:  # документ (в т. ч. загруженный как «созвон»)
        from app.app.services.storage import artifact_knowledge_text

        doc_text = artifact_knowledge_text(settings, artifact) or ""
    from app.app.services.doc_units import split_units

    doc_units = split_units(doc_text) if doc_text else []
    drafts: list[tuple[str, str, float]] = []

    def draft(entity_type: str, value: Any, confidence: float = 0.6) -> None:
        item = value
        if entity_type in CALL_BOUND_TYPES and chat_messages and isinstance(value, dict):
            # переписка: сообщение, автор, время (этап 2); решение из ответа на вопрос — источник ответ
            item = bind_chat_source(value, chat_messages, chat_skip, answer_follows=entity_type != "open_question")
            if item is None:
                return
        elif entity_type in CALL_BOUND_TYPES:
            item = bind_call_source(value, artifact, replicas, leaks, turns=turns, recorded_at=recorded_at)
            if item is None:
                return
        if isinstance(item, dict):
            item = dict(item)
            item.pop("turns", None)  # номера реплик нестабильны (после «Слить»); якорь — start_sec
            item.pop("messages", None)  # переписка: якорь — message_id
            if entity_type in HUMAN_TYPES and doc_text:
                item = bind_document_source(item, doc_units, doc_text)
            else:
                item.pop("quote", None)
                item.pop("paragraphs", None)
            if not item.get("decided_by"):
                item.pop("decided_by", None)
            if entity_type in HUMAN_TYPES:
                item = _clean_value(item)
        drafts.append((entity_type, json.dumps(item, ensure_ascii=False), confidence))

    for d in payload.decisions:
        draft("decision", _decision_value(d))
    for t in payload.tasks:
        draft("task", t.model_dump())
    for q in payload.open_questions:
        if q.text.strip():
            draft("open_question", q.model_dump())
    for k, v in payload.facts.items():
        draft("fact", {"key": k, "value": v})
    for m in payload.mentions:
        draft("mention", m.model_dump())
    for p in payload.participants:
        draft("participant", p.model_dump())
    if payload.summary:
        draft("summary", {"text": payload.summary}, 0.7)

    texts = [entity_text(vj) for _, vj, _ in drafts]
    vectors: list[list[float]] = []
    if texts and not settings.dry_run:
        init_vector_store(get_engine())
        vectors = embed_texts(settings, texts)
        if len(vectors) != len(texts):
            raise ValueError(f"ollama returned {len(vectors)} embeddings for {len(texts)} entities")

    human = _human_snapshot(db, artifact)
    old_ids = list(db.scalars(select(Entity.id).where(Entity.artifact_id == artifact.id)).all())
    if old_ids:
        delete_vectors_for_entities(db, old_ids)
        delete_entities_with_deps(db, old_ids)
        from app.app.services.graph import purge_orphan_links

        purge_orphan_links(db, project_id=artifact.project_id)

    rows: list[Entity] = []
    for entity_type, value_json, confidence in drafts:
        ent = Entity(
            artifact_id=artifact.id,
            entity_type=entity_type,
            value_json=value_json,
            confidence=confidence,
            is_active=True,
            needs_review=True,
        )
        db.add(ent)
        rows.append(ent)
    db.flush()
    if vectors:
        upsert_entity_vectors(db, list(zip((r.id for r in rows), vectors, strict=True)))
    if human:
        moved, missing = _transfer_human(rows, human)
        _write_transfer_report(settings, artifact, moved, missing)
        log_event(logger, "entities.human_transferred", artifact=artifact.artifact_id, transferred=moved, not_found=len(missing))

    out_dir = structured_dir(settings, artifact.project_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{artifact.artifact_id}_entities.json"
    out_path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
    return out_path


def _count_tasks_with_deadline(payload: EntitiesPayload) -> int:
    return sum(1 for task in payload.tasks if _normalize_deadline_value(task.deadline))


def reextract_artifact_entities(db: Session, settings: Settings, artifact: Artifact) -> dict:
    """Повторно извлечь сущности из канона lab ``_final.txt`` / transcription_text."""
    from app.app.services.gpu_phase import prepare_for_llm
    from app.app.services.storage import artifact_knowledge_text

    text = artifact_knowledge_text(settings, artifact)
    if not text:
        return {
            "ok": False,
            "artifact_id": artifact.artifact_id,
            "error": "нет текста стенограммы",
            "tasks": 0,
            "tasks_with_deadline": 0,
        }
    if settings.dry_run:
        payload = stub_entities(artifact, text)
    else:
        prepare_for_llm(settings.lmstudio_model, settings=settings)
        from app.app.services.extraction_context import build_extraction_context

        context = build_extraction_context(db, settings, artifact)
        # Закрыть транзакцию: ответ модели идёт десятки секунд, а соединение из пула всё это время было бы занято.
        db.commit()
        payload = extract_entities_llm(settings, artifact, text, context=context)
    persist_entities(db, settings, artifact, payload)
    db.commit()
    return {
        "ok": True,
        "artifact_id": artifact.artifact_id,
        "tasks": len(payload.tasks),
        "tasks_with_deadline": _count_tasks_with_deadline(payload),
    }


def reextract_project_entities(db: Session, settings: Settings, project_id: int) -> dict:
    """Все готовые материалы проекта: сущности из lab-стенограммы, не из Whisper/T-one."""
    artifacts = db.scalars(
        select(Artifact).where(Artifact.project_id == project_id, Artifact.status == "done")
    ).all()
    results: list[dict] = []
    succeeded = 0
    tasks_with_deadline = 0
    for artifact in artifacts:
        try:
            row = reextract_artifact_entities(db, settings, artifact)
        except Exception as exc:
            logger.exception("reextract failed for %s", artifact.artifact_id)
            row = {
                "ok": False,
                "artifact_id": artifact.artifact_id,
                "error": str(exc),
                "tasks": 0,
                "tasks_with_deadline": 0,
            }
        results.append(row)
        if row.get("ok"):
            succeeded += 1
            tasks_with_deadline += int(row.get("tasks_with_deadline") or 0)
    return {
        "project_id": project_id,
        "artifacts": len(artifacts),
        "succeeded": succeeded,
        "tasks_with_deadline": tasks_with_deadline,
        "results": results,
    }
