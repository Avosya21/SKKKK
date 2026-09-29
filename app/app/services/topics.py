"""Темы проекта (docs/plans/REQUIREMENTS-PLAN.md, этап 5, решение А2): модель раскладывает решения, задачи и открытые
вопросы по темам — потребностям или объектам, а не способам решения. По векторам тему не выделить (замер «ПИЛОТА»:
близость «приложение ↔ нормализация номенклатуры» 0.41–0.66 — на уровне фона проекта), поэтому раскладывает модель.

Подсказка темы из извлечения (``value.topic``), совпадающая с названием существующей темы, принимается без модели.
Новые пункты в теме сбрасывают ``Topic.reviewed_at`` — тему заново проверит актуальность (этап 7).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity, Topic

logger = logging.getLogger("skkkk.review")

ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "assign_topics.md"
PROMPT_VERSION = "assign_topics-v1"
TOPIC_TYPES = ("decision", "task", "open_question")
BATCH = 60
LABEL_MAX = 120
OTHER_LABEL = "Прочее"
KIND_LABELS = {"decision": "решение", "task": "задача", "open_question": "вопрос"}


def norm_label(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").strip().strip(".").casefold())


def _value(ent: Entity) -> dict[str, Any]:
    try:
        value = json.loads(ent.value_json or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: dict[str, Any]) -> str:
    return str(value.get("text") or value.get("description") or "").strip()


def project_topics(db: Session, project_id: int) -> list[Topic]:
    return list(db.scalars(select(Topic).where(Topic.project_id == project_id).order_by(Topic.id)).all())


def _get_or_create(db: Session, project_id: int, label: str, by_norm: dict[str, Topic]) -> Topic:
    key = norm_label(label)
    topic = by_norm.get(key)
    if topic is None:
        topic = Topic(project_id=project_id, label=label.strip()[:LABEL_MAX] or OTHER_LABEL, prompt_version=PROMPT_VERSION)
        db.add(topic)
        db.flush()
        by_norm[key] = topic
    return topic


def _item_line(ent: Entity, value: dict[str, Any], art: Artifact | None) -> str:
    from app.app.services.recorded_at import material_date

    day = material_date(art)[0] if art is not None else None
    hint = f" (подсказка темы: {value['topic']})" if value.get("topic") else ""
    return f"E{ent.id} [{day.strftime('%d.%m.%Y') if day else 'дата неизвестна'}] [{KIND_LABELS.get(ent.entity_type, ent.entity_type)}] {_text(value)[:300]}{hint}"


def _ask(settings: Settings, system: str, user: str) -> dict[str, Any]:
    from app.app.services.llm import chat_completion_no_think, strip_json_fence

    prompt, last = user, ""
    for attempt in range(2):
        reply = chat_completion_no_think(settings, system=system, user=prompt, max_tokens=4096)
        try:
            data = json.loads(strip_json_fence(reply))
            if isinstance(data, dict):
                return data
            last = "не объект"
        except json.JSONDecodeError as exc:
            last = str(exc)[:200]
        if attempt == 0:
            prompt = user + f"\n\nПредыдущий ответ невалиден: {last}. Верни только JSON по формату."
    raise RuntimeError(f"ответ модели не разобран: {last}")


def pending_entities(db: Session, project_id: int, artifact: Artifact | None = None) -> list[Entity]:
    q = (select(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
         .where(Artifact.project_id == project_id, Entity.is_active.is_(True), Entity.entity_type.in_(TOPIC_TYPES),
                Entity.topic_id.is_(None)))
    if artifact is not None:
        q = q.where(Entity.artifact_id == artifact.id)
    return list(db.scalars(q.order_by(Entity.id)).all())


def assign_topics(db: Session, settings: Settings, project_id: int, *, artifact: Artifact | None = None) -> dict[str, Any]:
    """Разложить по темам пункты без темы (артефакта или всего проекта). Вернуть сводку."""
    from app.app.services.prompts import load_prompt_block

    started = time.monotonic()
    stats = {"total": 0, "by_hint": 0, "by_model": 0, "new_topics": 0, "unassigned": 0, "llm_calls": 0, "llm_errors": 0}
    ents = pending_entities(db, project_id, artifact)
    stats["total"] = len(ents)
    if not ents:
        return {**stats, "seconds": 0.0}
    topics = project_topics(db, project_id)
    by_norm = {norm_label(t.label): t for t in topics}
    touched: set[int] = set()
    left: list[Entity] = []
    for ent in ents:
        value = _value(ent)
        hinted = by_norm.get(norm_label(str(value.get("topic") or "")))
        if hinted is not None:
            ent.topic_id = hinted.id
            touched.add(hinted.id)
            stats["by_hint"] += 1
        else:
            left.append(ent)
    db.commit()
    if left and not settings.dry_run:
        system = load_prompt_block(PROMPT_PATH)
        arts = {a.id: a for a in db.scalars(select(Artifact).where(Artifact.id.in_({e.artifact_id for e in left}))).all()}
        for start in range(0, len(left), BATCH):
            batch = left[start:start + BATCH]
            topics_text = "\n".join(f"t{t.id}: {t.label}" for t in by_norm.values()) or "(тем пока нет)"
            lines = "\n".join(_item_line(e, _value(e), arts.get(e.artifact_id)) for e in batch)
            db.commit()  # соединение БД не держим на время ответа модели (DECISIONS 2026-09-21)
            try:
                data = _ask(settings, system, f"Существующие темы:\n{topics_text}\n\nПункты:\n{lines}")
            except Exception as exc:  # noqa: BLE001 — пункты останутся без темы, следующий прогон повторит
                stats["llm_errors"] += 1
                log_event(logger, "topics.llm_failed", project=project_id, error=str(exc)[:300])
                continue
            stats["llm_calls"] += 1
            by_id = {f"E{e.id}": e for e in batch}
            known_ids = {f"t{t.id}": t for t in by_norm.values()}
            for item in data.get("items") or []:
                if not isinstance(item, dict):
                    continue
                ent = by_id.pop(str(item.get("id") or "").strip(), None)
                raw = str(item.get("topic") or "").strip()
                if ent is None or not raw:
                    continue
                if raw in known_ids:
                    topic = known_ids[raw]
                else:
                    label = raw.split(":", 1)[1].strip() if raw.lower().startswith("new") and ":" in raw else raw
                    before = len(by_norm)
                    topic = _get_or_create(db, project_id, label, by_norm)
                    stats["new_topics"] += len(by_norm) - before
                    known_ids[f"t{topic.id}"] = topic
                ent.topic_id = topic.id
                touched.add(topic.id)
                stats["by_model"] += 1
            stats["unassigned"] += len(by_id)
            db.commit()
    elif left:
        stats["unassigned"] = len(left)
    for topic in by_norm.values():
        if topic.id in touched:
            topic.reviewed_at = None  # в теме новые пункты — актуальность темы проверяется заново
    db.commit()
    stats["seconds"] = round(time.monotonic() - started, 1)
    log_event(logger, "topics.assigned", project=project_id, artifact=artifact.artifact_id if artifact else None, **stats)
    return stats


MERGE_PROMPT_PATH = ROOT / "docs" / "prompts" / "merge_topics.md"
MERGE_SAMPLE_ITEMS = 10  # замер «ПИЛОТА»: с 4 примерами склейка не увидела «вариант B: цвет в карточке, длина в строке»
# Темы, уже просмотренные склейкой (настройка проекта). Склеивается только группа с новой темой: иначе каждый прогон
# модель находила что-то ещё и темы сливались раз за разом (замер «ПИЛОТА»: 22 → 20 → 18 → 16).
MERGE_SEEN_KEY = "topics_merge_seen"


def _merge_seen(db: Session, project_id: int, topic_ids: list[int] | None = None) -> set[int]:
    """Прочитать (или записать, если ``topic_ids`` задан) темы, уже просмотренные склейкой."""
    from app.app.models.project import Project

    project = db.get(Project, project_id)
    if project is None:
        return set()
    cfg = project.settings()
    if topic_ids is not None:
        cfg[MERGE_SEEN_KEY] = sorted(topic_ids)
        project.set_settings(cfg)
    return {int(x) for x in cfg.get(MERGE_SEEN_KEY) or [] if str(x).isdigit()}


def merge_topics(db: Session, settings: Settings, project_id: int) -> dict[str, Any]:
    """Склеить темы одной потребности, разведённые по способу решения (``merge_topics.md``). Пункты переходят в первую тему
    группы, она получает общее название и проверяется на актуальность заново; остальные темы удаляются."""
    from app.app.services.prompts import load_prompt_block

    stats: dict[str, Any] = {"topics": 0, "groups": 0, "merged": 0, "llm_calls": 0, "llm_errors": 0}
    topics = project_topics(db, project_id)
    stats["topics"] = len(topics)
    fresh = {t.id for t in topics} - _merge_seen(db, project_id)
    if len(topics) < 2 or settings.dry_run or not fresh:
        return stats
    lines = []
    for topic in topics:
        ents = db.scalars(select(Entity).where(Entity.topic_id == topic.id, Entity.is_active.is_(True)).order_by(Entity.id)
                          .limit(MERGE_SAMPLE_ITEMS)).all()
        lines.append(f"t{topic.id}: {topic.label}")
        lines += [f"   - {_text(_value(e))[:160]}" for e in ents]
    db.commit()  # соединение БД не держим на время ответа модели
    try:
        data = _ask(settings, load_prompt_block(MERGE_PROMPT_PATH), "\n".join(lines))
    except Exception as exc:  # noqa: BLE001 — без склейки темы остаются как есть
        stats["llm_errors"] += 1
        log_event(logger, "topics.merge_failed", project=project_id, error=str(exc)[:300])
        return stats
    stats["llm_calls"] += 1
    by_id = {f"t{t.id}": t for t in topics}
    used: set[int] = set()
    for group in data.get("groups") or []:
        if not isinstance(group, dict):
            continue
        members = [by_id[t] for t in dict.fromkeys(str(x).strip() for x in group.get("topics") or []) if t in by_id]
        members = [t for t in members if t.id not in used]
        if len(members) < 2 or not any(t.id in fresh for t in members):
            continue  # старые темы между собой уже просмотрены — не склеиваем их при каждом прогоне
        keep, rest = members[0], members[1:]
        label = str(group.get("label") or "").strip()[:LABEL_MAX]
        if label:
            keep.label = label
        keep.reviewed_at = None
        for topic in rest:
            db.query(Entity).filter(Entity.topic_id == topic.id).update({Entity.topic_id: keep.id}, synchronize_session=False)
            db.delete(topic)
        used |= {t.id for t in members}
        stats["groups"] += 1
        stats["merged"] += len(rest)
    db.flush()
    _merge_seen(db, project_id, [t.id for t in project_topics(db, project_id)])
    db.commit()
    log_event(logger, "topics.merged", project=project_id, **stats)
    return stats


def review_artifact_topics(db: Session, settings: Settings, artifact: Artifact, mode: str) -> dict[str, Any]:
    """Исполнитель очереди (scope ``topics``): раскладка пунктов материала, затем склейка тем одной потребности.
    Темы не выключают пунктов, поэтому режим (тень / live) не важен."""
    stats = assign_topics(db, settings, artifact.project_id, artifact=artifact)
    stats["merge"] = merge_topics(db, settings, artifact.project_id)
    return stats


def topic_labels(db: Session, topic_ids: set[int]) -> dict[int, str]:
    if not topic_ids:
        return {}
    return {t.id: t.label for t in db.scalars(select(Topic).where(Topic.id.in_(topic_ids))).all()}
