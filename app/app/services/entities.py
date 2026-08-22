from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity
from app.app.logging_setup import log_event
from app.app.llm_labels import llm_runtime_label
from app.app.services.llm import chat_completion, strip_json_fence
from app.app.services.storage import structured_dir

logger = logging.getLogger("skkkk.entities")

ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "extract_entities.md"


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


def _merge_payloads(parts: list[EntitiesPayload]) -> EntitiesPayload:
    if not parts:
        return EntitiesPayload()
    base = parts[0].model_copy(deep=True)
    seen_decisions = set(base.decisions)
    seen_tasks: set[str] = {t.description for t in base.tasks}
    for part in parts[1:]:
        if part.summary and len(part.summary) > len(base.summary):
            base.summary = part.summary
        for d in part.decisions:
            if d not in seen_decisions:
                base.decisions.append(d)
                seen_decisions.add(d)
        for t in part.tasks:
            if t.description not in seen_tasks:
                base.tasks.append(t)
                seen_tasks.add(t.description)
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
    return base


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
    parts: list[EntitiesPayload] = []
    for chunk in _split_for_extraction(text):
        user = (
            f"artifact_id={artifact.artifact_id}\n"
            f"source_type={artifact.source_type}\n"
            f"timestamp={datetime.now(timezone.utc).isoformat()}\n\n"
            f"{chunk}"
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


def persist_entities(
    db: Session,
    settings: Settings,
    artifact: Artifact,
    payload: EntitiesPayload,
) -> Path:
    db.execute(delete(Entity).where(Entity.artifact_id == artifact.id))
    rows: list[Entity] = []

    def add(entity_type: str, value: Any, confidence: float = 0.6) -> None:
        ent = Entity(
            artifact_id=artifact.id,
            entity_type=entity_type,
            value_json=json.dumps(value, ensure_ascii=False),
            confidence=confidence,
            is_active=True,
            needs_review=True,
        )
        db.add(ent)
        rows.append(ent)

    for d in payload.decisions:
        add("decision", {"text": d})
    for t in payload.tasks:
        add("task", t.model_dump())
    for k, v in payload.facts.items():
        add("fact", {"key": k, "value": v})
    for m in payload.mentions:
        add("mention", m.model_dump())
    for p in payload.participants:
        add("participant", p.model_dump())
    if payload.summary:
        add("summary", {"text": payload.summary}, confidence=0.7)

    out_dir = structured_dir(settings, artifact.project_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{artifact.artifact_id}_entities.json"
    out_path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
    db.flush()
    return out_path
