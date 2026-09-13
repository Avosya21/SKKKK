from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.db import get_engine
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.logging_setup import log_event
from app.app.llm_labels import llm_runtime_label
from app.app.services.graph import delete_entities_with_deps, entity_text
from app.app.services.llm import chat_completion, strip_json_fence
from app.app.services.ollama_client import embed_texts
from app.app.services.storage import structured_dir
from app.app.services.vector_index import (
    delete_vectors_for_entities,
    init_vector_store,
    upsert_entity_vectors,
)

logger = logging.getLogger("skkkk.entities")

ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "extract_entities.md"
SOURCE_TYPE_CALL = "call"
TEXT_HASH_LEN = 16
CALL_BOUND_TYPES = {"decision", "task", "fact"}
TURN_PROMPT_LIMIT = 120
REPLICA_MATCH_WINDOW = 3
TEXT_OVERLAP_MIN_RATIO = 0.35


class TaskItem(BaseModel):
    description: str = ""
    assignee: str | None = None
    deadline: str | None = None
    status: str = "unknown"


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
    decisions: list[str] = Field(default_factory=list)
    tasks: list[TaskItem] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    mentions: list[MentionItem] = Field(default_factory=list)
    participants: list[ParticipantItem] = Field(default_factory=list)


def _load_system_prompt() -> str:
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    blocks = [b.strip() for b in raw.split("```") if b.strip() and not b.strip().startswith("#")]
    return blocks[0] if blocks else raw


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

    for decision in out.decisions:
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
                    assignee=None,
                    deadline=deadline,
                    status="open",
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


def _pick_assignee(current: str | None, incoming: str | None) -> str | None:
    cur = (current or "").strip()
    inc = (incoming or "").strip()
    if not inc:
        return cur or None
    if not cur:
        return inc
    if _SPEAKER_LABEL.match(cur) and not _SPEAKER_LABEL.match(inc):
        return inc
    if len(inc) > len(cur) and cur.casefold() in {"акме", "acme", "команда"}:
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
    seen_decisions = set(base.decisions)
    merged_tasks: list[TaskItem] = list(base.tasks)
    for part in parts[1:]:
        if part.summary and len(part.summary) > len(base.summary):
            base.summary = part.summary
        for d in part.decisions:
            if d not in seen_decisions:
                base.decisions.append(d)
                seen_decisions.add(d)
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


def _parse_entities(raw: str) -> EntitiesPayload:
    cleaned = strip_json_fence(raw)
    data = json.loads(cleaned)
    return EntitiesPayload.model_validate(data)


def extract_entities_llm(settings: Settings, artifact: Artifact, text: str) -> EntitiesPayload:
    system = _load_system_prompt()
    log_event(
        logger,
        "entities.llm.start",
        artifact=artifact.artifact_id,
        llm_provider=settings.llm_provider,
        llm=llm_runtime_label(settings.llm_provider),
        chars=len(text),
    )
    turns = _turn_prompt(settings, artifact)
    parts: list[EntitiesPayload] = []
    for chunk in _split_for_extraction(text):
        user = (
            f"artifact_id={artifact.artifact_id}\n"
            f"source_type={artifact.source_type}\n"
            f"timestamp={datetime.now(timezone.utc).isoformat()}\n\n"
            f"{turns}{chunk}"
        )
        last_err = ""
        for attempt in range(2):
            try:
                reply = chat_completion(settings, system=system, user=user, max_tokens=4096)
                payload = _parse_entities(reply)
                payload.artifact_id = artifact.artifact_id
                payload.source_type = artifact.source_type
                parts.append(payload)
                break
            except (json.JSONDecodeError, ValidationError, RuntimeError) as exc:
                last_err = str(exc)
                if attempt == 0:
                    user = user + f"\n\nПредыдущий ответ невалиден: {last_err}. Верни только JSON."
                else:
                    raise RuntimeError(f"entity extraction failed: {last_err}") from exc
    return _merge_payloads(parts)


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


def _turn_prompt(settings: Settings, artifact: Artifact) -> str:
    if getattr(artifact, "source_type", "") != SOURCE_TYPE_CALL:
        return ""
    replicas, _leaks = load_meeting_proof(settings, artifact)
    lines = []
    for row in replicas[:TURN_PROMPT_LIMIT]:
        sid = str(row.get("source_span_id") or "").strip()
        text = _replica_text(row)
        if sid and text:
            lines.append(f"{sid}\t{row.get('speaker')}\t{text}")
    if not lines:
        return ""
    return (
        "Реплики с source_span_id. decision/task/fact только из этого списка.\n"
        + "\n".join(lines)
        + "\n\n"
    )


def filter_call_payload(
    payload: EntitiesPayload,
    artifact: Artifact,
    replicas: list[dict],
    leaks: list[dict],
) -> EntitiesPayload:
    data = payload.model_copy(deep=True)
    data.decisions = [
        item for item in payload.decisions if bind_call_source({"text": item}, artifact, replicas, leaks)
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


def bind_call_source(
    value: dict,
    artifact: Artifact,
    replicas: list[dict],
    leaks: list[dict],
) -> dict | None:
    if getattr(artifact, "source_type", "") != SOURCE_TYPE_CALL:
        return value
    text = str(value.get("text") or value.get("description") or value.get("value") or "")
    if _leak_covers(text, leaks):
        return None
    if not _has_span_replicas(replicas):
        return value
    replica = _match_replica(text, replicas)
    bound = dict(value)
    if replica is None:
        bound["source_span_id"] = None
        bound["needs_review"] = True
        return bound
    bound["artifact_id"] = artifact.artifact_id
    bound["source_type"] = SOURCE_TYPE_CALL
    bound["speaker"] = replica.get("speaker")
    bound["start_sec"] = replica.get("start")
    bound["end_sec"] = replica.get("end")
    bound["source_span_id"] = replica.get("source_span_id")
    bound["text_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:TEXT_HASH_LEN]
    return bound


def persist_entities(
    db: Session,
    settings: Settings,
    artifact: Artifact,
    payload: EntitiesPayload,
) -> Path:
    payload.tasks = merge_related_tasks(payload.tasks)
    replicas, leaks = ([], [])
    if getattr(artifact, "source_type", "") == SOURCE_TYPE_CALL:
        replicas, leaks = load_meeting_proof(settings, artifact)
        if _has_span_replicas(replicas):
            payload = filter_call_payload(payload, artifact, replicas, leaks)
    drafts: list[tuple[str, str, float]] = []

    def draft(entity_type: str, value: Any, confidence: float = 0.6) -> None:
        item = value
        if entity_type in CALL_BOUND_TYPES:
            item = bind_call_source(value, artifact, replicas, leaks)
            if item is None:
                return
        drafts.append((entity_type, json.dumps(item, ensure_ascii=False), confidence))

    for d in payload.decisions:
        draft("decision", {"text": d})
    for t in payload.tasks:
        draft("task", t.model_dump())
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
        payload = extract_entities_llm(settings, artifact, text)
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
