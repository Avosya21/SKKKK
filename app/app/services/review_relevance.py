"""Актуальность пунктов по теме (docs/plans/REQUIREMENTS-PLAN.md, этапы 6–7; решения А1, А3–А6).

Модель читает пункты одной темы по хронологии (промпт ``review_relevance.md``) и говорит про каждый: в силе / заменён
более поздним / дубль / выполнен / неясно. Код проверяет ответ и применяет:
- **заменён** — заменивший позже по дате записи, обе даты известны (А4), заменивший подтверждается своим источником и не
  слабее по весу (Р1). Оформляется парой «заменённый ↔ заменивший» (``POTENTIAL_CONFLICT`` + ``apply_conflict_verdict``,
  правило ``rule:topic_superseded``): отсюда «Вернуть», «Важное», хронология в чате и ТЗ, автооткат. Подтверждённое
  человеком — по А6: подтверждено до появления заменяющего материала → замена применяется и попадает во «Важное»;
  после → пункт не трогаем;
- **дубль** — выключается с ``merged_into`` (его источники показываются у основного); подтверждённый человеком не трогается;
- **выполнен** — только отметка (А5).
Вердикт хранится в ``value.relevance`` (отдельно от ``model_review`` проверки требования). В тени пункты не выключаются,
но «заменённое» и «дубли» прячутся из «Ждут вас» на свои вкладки (А3).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.entity import Entity, Link, Resolution, Topic
from app.app.services.auto_review import MODE_LIVE, MODE_SHADOW, record_model_review

logger = logging.getLogger("skkkk.review")

ROOT = Path(__file__).resolve().parents[3]
PROMPT_PATH = ROOT / "docs" / "prompts" / "review_relevance.md"
PROMPT_VERSION = "review_relevance-v1"
MIN_CONFIDENCE = 0.8
STATES = ("current", "superseded", "duplicate", "done", "unclear")
HIDDEN_STATES = ("superseded", "duplicate")  # прячутся из «Ждут вас» уже в тени (А3)
MAX_ITEMS = 40
QUOTE_CHARS = 300
RELEVANCE_TYPES = ("decision", "task", "open_question")
KIND_LABELS = {"decision": "решение", "task": "задача", "open_question": "вопрос"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(ent: Entity) -> dict[str, Any]:
    try:
        value = json.loads(ent.value_json or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def relevance_of(value: dict[str, Any]) -> dict[str, Any] | None:
    rel = value.get("relevance") if isinstance(value, dict) else None
    return rel if isinstance(rel, dict) else None


def hidden_by_relevance(value: dict[str, Any]) -> str | None:
    """Состояние, из-за которого пункт не ждёт человека (А3): superseded / duplicate, не отменённое человеком."""
    rel = relevance_of(value)
    if rel and rel.get("state") in HIDDEN_STATES and not rel.get("undone"):
        return str(rel["state"])
    return None


def _write_relevance(ent: Entity, rel: dict[str, Any]) -> None:
    value = _value(ent)
    previous = relevance_of(value)
    if previous and previous.get("undone"):
        return  # человек сказал «это актуально» — повторно модель не перебивает
    value["relevance"] = rel
    ent.value_json = json.dumps(value, ensure_ascii=False)


def _items(db: Session, settings: Settings, topic: Topic, cache: dict) -> list[dict[str, Any]]:
    from app.app.services.conflict_rules import source_weight
    from app.app.services.project_deadlines import artifact_reference_date
    from app.app.services.recorded_at import item_moment
    from app.app.services.review_conflicts import _source
    from app.app.services.review_requirements import _statement

    rows = db.execute(
        select(Entity, Artifact).join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Entity.topic_id == topic.id, Entity.is_active.is_(True), Entity.entity_type.in_(RELEVANCE_TYPES))
    ).all()
    out = []
    for ent, art in rows:
        value = _value(ent)
        text = _statement(ent.entity_type, value) or str(value.get("text") or "")
        day, moment = item_moment(art, value)  # переписка — время сообщения, созвон с временем начала — момент (этап 3)
        who = value.get("speaker") or ""
        agreed = ", ".join(str(d.get("name")) for d in value.get("decided_by") or [] if isinstance(d, dict) and d.get("name"))
        try:
            quote = " ".join(_source(settings, ent, art, value, cache))[:QUOTE_CHARS]
        except Exception:  # noqa: BLE001 — без цитаты пункт всё равно участвует
            quote = ""
        out.append({"ent": ent, "art": art, "value": value, "text": text, "day": day, "moment": moment,
                    "order": (day or artifact_reference_date(art) or date.min, moment.timestamp() if moment else 0.0,
                              float(value.get("start_sec") or 0.0), ent.id),
                    "weight": source_weight(ent.entity_type, value, art, text), "who": who, "agreed": agreed, "quote": quote,
                    "human": value.get("human_review"), "undone": bool((relevance_of(value) or {}).get("undone"))})
    out.sort(key=lambda it: it["order"])
    return out[-MAX_ITEMS:]


def _prompt(topic: Topic, items: list[dict[str, Any]]) -> str:
    from app.app.services.conflict_rules import WEIGHT_LABELS

    lines = [f"Тема: {topic.label}", ""]
    for it in items:
        ent = it["ent"]
        who = f"сказал: {it['who']}" if it["who"] else "говорящий не опознан"
        if it["agreed"]:
            who += f"; утвердил: {it['agreed']}"
        if it["moment"] is not None:
            from app.app.services.recorded_at import LOCAL_TZ

            day = it["moment"].astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")
        else:
            day = it["day"].strftime("%d.%m.%Y") if it["day"] else "дата неизвестна"
        lines.append(f"E{ent.id} | {day} | {KIND_LABELS.get(ent.entity_type, ent.entity_type)} | вес: {WEIGHT_LABELS.get(it['weight'], it['weight'])} | {who}")
        lines.append(f"   {it['text']}")
        if it["quote"]:
            lines.append(f"   цитата: «{it['quote']}»")
    return "\n".join(lines)


def _ask(settings: Settings, system: str, user: str) -> dict[str, Any]:
    from app.app.services.topics import _ask as ask_json

    return ask_json(settings, system, user)


def _later(a: dict[str, Any], b: dict[str, Any]) -> bool | None:
    """Позже ли ``b`` чем ``a``. None — нельзя сказать: нет даты (А4) или один день без времени у одной из сторон (Я3)."""
    if a["moment"] is not None and b["moment"] is not None:  # сообщения переписки, созвоны с известным временем начала
        return None if a["moment"] == b["moment"] else b["moment"] > a["moment"]
    if a["art"].id == b["art"].id and a["value"].get("start_sec") is not None and b["value"].get("start_sec") is not None:
        return float(b["value"]["start_sec"]) > float(a["value"]["start_sec"])
    if a["day"] is None or b["day"] is None or a["day"] == b["day"]:
        return None
    return b["day"] > a["day"]


def _human_before(item: dict[str, Any], by: dict[str, Any]) -> bool:
    """А6: человек подтвердил пункт ДО того, как в проект пришёл заменяющий материал (он не мог знать об отказе)."""
    human = item["human"] or {}
    try:
        at = datetime.fromisoformat(str(human.get("at")))
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    uploaded = by["art"].upload_date
    if uploaded is None:
        return False
    if uploaded.tzinfo is None:
        uploaded = uploaded.replace(tzinfo=timezone.utc)
    return at < uploaded


def _link_for(db: Session, project_id: int, a: Entity, b: Entity, label: str) -> Link:
    link = db.scalar(select(Link).where(Link.link_type == "POTENTIAL_CONFLICT", or_(
        (Link.source_entity_id == a.id) & (Link.target_entity_id == b.id),
        (Link.source_entity_id == b.id) & (Link.target_entity_id == a.id))))
    if link is None:
        link = Link(project_id=project_id, source_entity_id=b.id, target_entity_id=a.id, link_type="POTENTIAL_CONFLICT",
                    similarity_score=None, is_automatic=True, description=f"тема: {label}"[:500])
        db.add(link)
        db.flush()
    return link


def _apply_superseded(db: Session, settings: Settings, topic: Topic, item: dict, by: dict, verdict: dict, mode: str,
                      model: str, cache: dict) -> tuple[str, str]:
    """Применить замену. Вернуть (итог, заметка): applied / unclear."""
    from app.app.services.conflict_rules import cancel_allowed
    from app.app.services.review_conflicts import _pair, apply_conflict_verdict

    later = _later(item, by)
    if later is None:
        return "unclear", "нельзя понять, что сказано позже: у материала нет даты записи или оба материала одного дня (А4)"
    if not later:
        return "unclear", "заменивший пункт сказан раньше заменённого"
    over_human = False
    if item["human"]:
        if not _human_before(item, by):
            return "unclear", "подтверждено человеком уже после появления заменяющего материала (А6) — ждёт человека"
        over_human = True
    link = _link_for(db, topic.project_id, item["ent"], by["ent"], topic.label)
    if db.scalar(select(Resolution.id).where(Resolution.link_id == link.id)):
        return "unclear", "пара уже решена (человеком или раньше)"
    pair = _pair(db, settings, link, cache)
    if pair is None or pair["newer"]["id"] != by["ent"].id:
        return "unclear", "по датам заменивший пункт не позже заменённого"
    if not cancel_allowed(pair["newer"], pair["older"]):
        return "unclear", "заменивший пункт из более слабого источника (Р1)"
    quote = str(verdict.get("quote") or "")[:200]
    reason = f"тема «{topic.label}»: {verdict.get('reason') or 'заменено более поздним решением'}" + (f" — «{quote}»" if quote else "")
    final = apply_conflict_verdict(db, settings, pair, verdict="newer_wins", reason=reason, confidence=float(verdict["confidence"]),
                                   mode=mode, model=model, rule="rule:topic_superseded", relation="superseded",
                                   over_human=over_human, important=True if over_human else None)
    if final != "newer_wins":
        return "unclear", "проверка источника заменившего пункта не прошла"
    return "applied", "подтверждённое человеком заменено: см. «Важное»" if over_human else ""


def _apply_duplicate(item: dict, by: dict, verdict: dict, mode: str, model: str, settings: Settings) -> tuple[str, str]:
    if item["human"]:
        return "unclear", "подтверждено человеком: дубль не выключается (А6)"
    if mode == MODE_LIVE:
        record_model_review(item["ent"], settings, verdict="reject", reason=f"дубль: {verdict.get('reason') or ''}",
                            confidence=float(verdict["confidence"]), mode=MODE_LIVE, model=model, prompt_version=PROMPT_VERSION,
                            extra={"reject_kind": "duplicate", "merged_into": by["ent"].id, "previous": item["value"].get("model_review")})
    return "applied", ""


def review_topic(db: Session, settings: Settings, topic: Topic, mode: str, *, system: str, model: str, cache: dict) -> dict[str, int]:
    stats = {"items": 0, "current": 0, "superseded": 0, "duplicate": 0, "done": 0, "unclear": 0, "llm_calls": 0, "llm_errors": 0}
    items = _items(db, settings, topic, cache)
    stats["items"] = len(items)
    if len(items) < 2:
        topic.reviewed_at, topic.prompt_version = _now(), PROMPT_VERSION
        return stats
    user = _prompt(topic, items)
    db.commit()  # соединение БД не держим на время ответа модели (DECISIONS 2026-09-21)
    try:
        data = _ask(settings, system, user)
    except Exception as exc:  # noqa: BLE001 — тема останется непроверенной, следующий прогон повторит
        stats["llm_errors"] += 1
        log_event(logger, "relevance.llm_failed", topic=topic.id, error=str(exc)[:300])
        return stats
    stats["llm_calls"] += 1
    by_id = {f"E{it['ent'].id}": it for it in items}
    done_dups: set[int] = set()
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        item = by_id.get(str(raw.get("id") or "").strip())
        if item is None or item["undone"]:
            continue
        state = str(raw.get("state") or "").strip().lower()
        state = state if state in STATES else "unclear"
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        verdict = {"reason": str(raw.get("reason") or "")[:300], "quote": str(raw.get("quote") or "")[:300], "confidence": confidence}
        by = by_id.get(str(raw.get("by") or "").strip())
        note = ""
        if state in ("superseded", "duplicate", "done") and (by is None or by is item):
            state, note = "unclear", "не указан пункт, на котором основан вывод"
        elif state in ("superseded", "duplicate") and confidence < MIN_CONFIDENCE:
            state, note = "unclear", f"уверенность {confidence:.2f} ниже порога"
        elif state == "duplicate" and by["ent"].id in done_dups:
            state, note = "unclear", "пары дублей указывают друг на друга"
        if state == "superseded":
            outcome, note = _apply_superseded(db, settings, topic, item, by, verdict, mode, model, cache)
            state = "superseded" if outcome == "applied" else "unclear"
        elif state == "duplicate":
            outcome, note = _apply_duplicate(item, by, verdict, mode, model, settings)
            state = "duplicate" if outcome == "applied" else "unclear"
            if state == "duplicate":
                done_dups.add(item["ent"].id)
        rel = {"state": state, "by": by["ent"].id if by is not None and state in ("superseded", "duplicate", "done") else None,
               "topic_id": topic.id, "quote": verdict["quote"], "reason": verdict["reason"], "confidence": round(confidence, 3),
               "note": note or None, "mode": mode, "model": model, "prompt_version": PROMPT_VERSION, "at": _now()}
        _write_relevance(item["ent"], rel)
        stats[state] += 1
    topic.reviewed_at, topic.prompt_version = _now(), PROMPT_VERSION
    db.commit()
    return stats


def review_topics(db: Session, settings: Settings, project_id: int, mode: str, *, topic_ids: set[int] | None = None) -> dict[str, Any]:
    """Проверить темы проекта, где есть новые пункты (``reviewed_at`` пуст), или заданные темы."""
    from app.app.models.project import Project
    from app.app.services.prompts import load_prompt_block
    from app.app.services.review_conflicts import effective_mode
    from app.app.services.review_requirements import _model_label

    started = time.monotonic()
    project = db.get(Project, project_id)
    mode = effective_mode(project, mode) if project is not None else mode
    q = select(Topic).where(Topic.project_id == project_id)
    q = q.where(Topic.id.in_(topic_ids)) if topic_ids is not None else q.where(Topic.reviewed_at.is_(None))
    topics = list(db.scalars(q.order_by(Topic.id)).all())
    totals: dict[str, Any] = {"topics": len(topics), "mode": mode}
    if not topics:
        return {**totals, "seconds": 0.0}
    system = load_prompt_block(PROMPT_PATH)
    model = _model_label(settings)
    if not settings.dry_run:
        from app.app.services.gpu_phase import prepare_for_llm

        db.commit()
        prepare_for_llm(settings.lmstudio_model, settings=settings)
    cache: dict = {}
    for topic in topics:
        stats = review_topic(db, settings, topic, mode, system=system, model=model, cache=cache)
        for key, value in stats.items():
            totals[key] = totals.get(key, 0) + value
        db.commit()
    totals["seconds"] = round(time.monotonic() - started, 1)
    log_event(logger, "relevance.reviewed", project=project_id, **{k: v for k, v in totals.items() if k != "mode"}, mode=mode)
    return totals


def _project_busy(db: Session, project_id: int) -> int:
    """Сколько в очереди ещё ждут расшифровки / знаний / раскладки по темам этого проекта."""
    from sqlalchemy import func

    from app.app.models.job import Job

    return int(db.scalar(
        select(func.count()).select_from(Job).join(Artifact, Artifact.id == Job.artifact_id)
        .where(Artifact.project_id == project_id, Job.status == "queued",
               or_(Job.kind == "process_artifact", Job.payload_json.like('%"scope": "topics"%'),
                   Job.payload_json.like('%"scope": "reextract"%')))
    ) or 0)


def review_artifact_relevance(db: Session, settings: Settings, artifact: Artifact, mode: str) -> dict[str, Any]:
    """Исполнитель очереди (scope ``relevance``): все непроверенные темы проекта.

    Пока в очереди ждут материалы или раскладка по темам этого проекта, проверка откладывается: при переизвлечении всего
    проекта иначе каждая тема проверялась бы столько раз, сколько материалов в неё пишут. Последнее задание проверит всё.
    """
    busy = _project_busy(db, artifact.project_id)
    if busy:
        log_event(logger, "relevance.deferred", project=artifact.project_id, artifact=artifact.artifact_id, waiting=busy)
        return {"topics": 0, "mode": mode, "deferred": busy, "seconds": 0.0}
    return review_topics(db, settings, artifact.project_id, mode)


def mark_current(db: Session, entity_id: int, *, user_name: str, user_id: int | None) -> dict[str, Any]:
    """«Это актуально» (в тени): снять вердикт «заменено / дубль» — пункт снова ждёт человека, модель его больше не перебивает."""
    ent = db.get(Entity, entity_id)
    if ent is None:
        raise ValueError("пункт не найден")
    value = _value(ent)
    rel = relevance_of(value)
    if not rel or rel.get("undone"):
        raise ValueError("у пункта нет вердикта актуальности")
    rel["undone"] = {"by": user_name, "user_id": user_id, "at": _now()}
    value["relevance"] = rel
    ent.value_json = json.dumps(value, ensure_ascii=False)
    log_event(logger, "relevance.undo", entity=entity_id, state=rel.get("state"), by=user_name)
    return {"entity_id": entity_id, "state": rel.get("state"), "undone": True}
