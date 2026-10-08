"""Контекст для извлечения требований (docs/plans/REQUIREMENTS-PLAN.md, этап 3): карточка проекта, оглавление созвона,
темы проекта. Без контекста модель видела кусок созвона в 6000 знаков и формулировала пункт «вырванным» из разговора.

- Карточка проекта ``project_state/project_card.md``: цель, системы, этапы, участники со сторонами, термины. Черновик
  собирает модель из выжимки проекта, выжимок созвонов и участников; правка администратора главнее черновика
  (``project_card.json``: ``source`` = model | human) — пересборка черновика её не перетирает.
- Оглавление созвона ``parsed/calls/<artifact_id>_outline.json``: темы разговора с таймкодами.
- Темы проекта — из таблицы ``topics`` (этап 5).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity, Topic
from app.app.services.storage import parsed_dir, project_root

logger = logging.getLogger("skkkk.entities")

ROOT = Path(__file__).resolve().parents[3]
CARD_PROMPT = ROOT / "docs" / "prompts" / "project_card.md"
OUTLINE_PROMPT = ROOT / "docs" / "prompts" / "call_outline.md"
CARD_MAX_CHARS = 3000
OUTLINE_PART_CHARS = 20000  # оглавлению хватает крупных частей: часовой созвон — 2–3 вызова модели
OUTLINE_MAX_CHARS = 2500
TOPICS_MAX = 60
CARD_SOURCES_TZ_CHARS = 6000
CARD_SOURCES_SUMMARY_CHARS = 900
CARD_SOURCES_SUMMARIES = 12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def card_paths(settings: Settings, project_id: int) -> tuple[Path, Path]:
    base = project_root(settings, project_id) / "project_state"
    return base / "project_card.md", base / "project_card.json"


def read_project_card(settings: Settings, project_id: int) -> tuple[str, dict[str, Any]]:
    """(текст, мета). Нет карточки — ("", {})."""
    text_path, meta_path = card_paths(settings, project_id)
    text = text_path.read_text(encoding="utf-8").strip() if text_path.is_file() else ""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        meta = {}
    return text, meta if isinstance(meta, dict) else {}


def save_project_card(settings: Settings, project_id: int, text: str, *, source: str, by: str | None = None) -> dict[str, Any]:
    text_path, meta_path = card_paths(settings, project_id)
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(text.strip() + "\n", encoding="utf-8")
    meta = {"source": source, "by": by, "at": _now()}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _card_sources(db: Session, settings: Settings, project_id: int) -> str:
    from app.app.services.tz import tz_paths

    parts: list[str] = []
    tz_path, _ = tz_paths(settings, project_id)
    if tz_path.is_file():
        parts.append("## Выжимка проекта\n" + tz_path.read_text(encoding="utf-8")[:CARD_SOURCES_TZ_CHARS])
    arts = db.scalars(select(Artifact).where(Artifact.project_id == project_id, Artifact.status == "done").order_by(Artifact.id)).all()
    summaries = []
    for art in arts:
        path = parsed_dir(settings, project_id, art.source_type) / f"{art.artifact_id}_call_summary.md"
        if path.is_file():
            summaries.append(f"### {art.original_filename}\n" + path.read_text(encoding="utf-8")[:CARD_SOURCES_SUMMARY_CHARS])
    if summaries:
        parts.append("## Выжимки созвонов\n" + "\n\n".join(summaries[:CARD_SOURCES_SUMMARIES]))
    people = db.scalars(
        select(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.entity_type == "participant", Entity.is_active.is_(True))
    ).all()
    seen: set[str] = set()
    lines = []
    for ent in people:
        try:
            value = json.loads(ent.value_json)
        except json.JSONDecodeError:
            continue
        name = str(value.get("name") or "").strip() if isinstance(value, dict) else ""
        if name and name.lower() not in seen:
            seen.add(name.lower())
            lines.append(f"- {name}" + (f" — {value.get('role')}" if value.get("role") else ""))
    if lines:
        parts.append("## Участники из материалов\n" + "\n".join(lines[:40]))
    return "\n\n".join(parts)


def build_project_card_draft(db: Session, settings: Settings, project_id: int) -> str:
    """Черновик карточки моделью. Пустые материалы — пустая строка (модель не зовём)."""
    from app.app.services.llm import chat_completion_no_think
    from app.app.services.prompts import load_prompt_block

    sources = _card_sources(db, settings, project_id)
    if not sources.strip():
        return ""
    db.commit()  # соединение БД не держим на время ответа модели (DECISIONS 2026-09-21)
    reply = chat_completion_no_think(settings, system=load_prompt_block(CARD_PROMPT), user=sources, max_tokens=2048)
    return reply.strip()[:CARD_MAX_CHARS * 2]


def _tz_newer_than_card(settings: Settings, project_id: int) -> bool:
    from app.app.services.tz import tz_paths

    tz_path, _ = tz_paths(settings, project_id)
    card_path, _meta_path = card_paths(settings, project_id)
    return tz_path.is_file() and card_path.is_file() and tz_path.stat().st_mtime > card_path.stat().st_mtime


def ensure_project_card(db: Session, settings: Settings, project_id: int, *, force: bool = False) -> str:
    """Карточка для извлечения: существующая (любая) или новый черновик. ``force`` пересобирает только черновик модели —
    правку человека не трогает. Черновик модели сам обновляется, когда выжимка проекта (ТЗ) новее карточки: проект живёт,
    новые встречи и документы меняют этапы и участников."""
    text, meta = read_project_card(settings, project_id)
    if text and not force and not (meta.get("source") == "model" and _tz_newer_than_card(settings, project_id)):
        return text
    if meta.get("source") == "human":
        return text
    if settings.dry_run:
        return text
    try:
        draft = build_project_card_draft(db, settings, project_id)
    except Exception as exc:  # noqa: BLE001 — без карточки извлечение всё равно идёт, просто беднее контекстом
        log_event(logger, "project_card.failed", project=project_id, error=str(exc)[:300])
        return text
    if draft:
        save_project_card(settings, project_id, draft, source="model")
        log_event(logger, "project_card.built", project=project_id, chars=len(draft))
        return draft
    return text


def _outline_path(settings: Settings, artifact: Artifact) -> Path:
    return parsed_dir(settings, artifact.project_id, "call") / f"{artifact.artifact_id}_outline.json"


def outline_text(topics: list[dict[str, Any]]) -> str:
    lines = [f"[{t.get('start') or '?'}] {t.get('title') or ''}" + (f" — {t['outcome']}" if t.get("outcome") else "") for t in topics]
    return "\n".join(lines)[:OUTLINE_MAX_CHARS]


def call_outline(settings: Settings, artifact: Artifact, *, force: bool = False) -> str:
    """Оглавление созвона (строки «[мм:сс] тема — итог»). Кэш на диске; не созвон или нет реплик — пустая строка."""
    if artifact.file_type != "audio":
        return ""
    path = _outline_path(settings, artifact)
    if path.is_file() and not force:
        try:
            return outline_text(json.loads(path.read_text(encoding="utf-8")).get("topics") or [])
        except (OSError, json.JSONDecodeError):
            pass
    if settings.dry_run:
        return ""
    from app.app.services.entities import _numbered_turn_parts
    from app.app.services.llm import chat_completion_no_think, strip_json_fence
    from app.app.services.prompts import load_prompt_block

    parts = _numbered_turn_parts(settings, artifact, max_chars=OUTLINE_PART_CHARS)
    if not parts:
        return ""
    system = load_prompt_block(OUTLINE_PROMPT)
    artifact_key = artifact.artifact_id
    # Чтение полей artifact после коммита карточки проекта открывает новую транзакцию (ленивая подгрузка), и она висела
    # всё время ответов модели (worker.log 2026-10-05 11:58:49). Закрываем её до вызовов модели.
    from sqlalchemy.orm import object_session

    session = object_session(artifact)
    if session is not None:
        session.commit()
    topics: list[dict[str, Any]] = []
    for part in parts:
        data = None
        for attempt in (1, 2):  # кривой JSON у модели случаен: один повтор, иначе часть оглавления теряется
            try:
                data = json.loads(strip_json_fence(chat_completion_no_think(settings, system=system, user=part, max_tokens=2048)))
                break
            except Exception as exc:  # noqa: BLE001 — часть без оглавления не ломает извлечение
                log_event(logger, "call_outline.part_failed", artifact=artifact_key, attempt=attempt, error=str(exc)[:200])
        if data is None:
            continue
        for item in (data.get("topics") if isinstance(data, dict) else None) or []:
            if isinstance(item, dict) and item.get("title"):
                topics.append({"start": str(item.get("start") or ""), "title": str(item["title"])[:120],
                               "outcome": str(item.get("outcome") or "")[:240]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"topics": topics, "at": _now()}, ensure_ascii=False, indent=2), encoding="utf-8")
    log_event(logger, "call_outline.built", artifact=artifact_key, topics=len(topics))
    return outline_text(topics)


def known_topics_text(db: Session, project_id: int) -> str:
    labels = db.scalars(select(Topic.label).where(Topic.project_id == project_id).order_by(Topic.id).limit(TOPICS_MAX)).all()
    return "\n".join(f"- {label}" for label in labels)


def build_extraction_context(db: Session, settings: Settings, artifact: Artifact) -> str:
    """Блоки контекста для промпта извлечения v2. Строит карточку и оглавление, если их ещё нет (модель уже загружена)."""
    blocks: list[str] = []
    card = ensure_project_card(db, settings, artifact.project_id)
    if card:
        blocks.append("## Карточка проекта\n" + card[:CARD_MAX_CHARS])
    outline = call_outline(settings, artifact)
    if outline:
        blocks.append("## Оглавление созвона\n" + outline)
    topics = known_topics_text(db, artifact.project_id)
    if topics:
        blocks.append("## Темы проекта\n" + topics)
    return "\n\n".join(blocks)
