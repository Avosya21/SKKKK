"""Автопроверка конфликтов (POTENTIAL_CONFLICT) локальной моделью — этап 4 docs/plans/AUTO-REVIEW-PLAN.md,
v3 — docs/plans/CONFLICT-AUTONOMY-PLAN.md.

Пара сущностей с источниками, датами и весом источника уходит в LLM (промпт ``review_conflict_v3.md``). Модель
называет ОТНОШЕНИЕ пунктов (отмена, противоречие, уточнение, одно и то же, разные объекты, не решение, неверно
извлечено); КТО в силе, решает код по правилам ``conflict_rules`` (Р1). Итоговый вердикт с уверенностью ≥ порога:
- ``newer_wins`` / ``older_wins`` (live) → ``Resolution(status="model_resolved", resolved_by=NULL)``, проигравшая сторона
  выключается и получает ``model_review`` (reject, ``superseded``) — её видно в «Отклонены моделью» и можно «Вернуть»,
  даже если граф пересоберут и связь исчезнет;
- ``not_conflict`` (live) → ``Resolution(status="model_dismissed")``, никто не выключается;
- иначе (и в тени) — только ``Link.model_review``, конфликт ждёт человека.
Человек главнее: разрешённый человеком конфликт и подтверждённую человеком сторону модель не трогает.
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
from app.app.models.entity import Entity, Link, Resolution
from app.app.services.auto_review import MODE_LIVE, MODE_SHADOW, record_model_review, undo_model_review

logger = logging.getLogger("skkkk.review")

ROOT = Path(__file__).resolve().parents[3]
# Новый файл, не правка v2: работающий процесс читает промпт с диска на лету (AUTO-REVIEW-PLAN §2 п.8).
PROMPT_PATH = ROOT / "docs" / "prompts" / "review_conflict_v3.md"
PROMPT_VERSION = "review_conflict-v3"
MIN_CONFIDENCE = 0.8
VERDICTS = ("newer_wins", "older_wins", "not_conflict", "unsure")
RELATIONS = ("replaces", "contradicts", "refines", "same", "different_objects", "not_decision", "misextracted", "unsure")
# Автооткат (этап 7): доля «Вернуть» среди авторешений проекта за окно выше порога → конфликты проекта уходят в тень.
UNDO_WINDOW_DAYS = 14
UNDO_MIN_DECISIONS = 10
UNDO_MAX_RATE = 0.05
PROJECT_MODE_KEY = "conflict_review_mode"
MODEL_STATUSES = ("model_resolved", "model_dismissed")
CONFLICT = "POTENTIAL_CONFLICT"
# Противоречить могут только решения, задачи и факты (как в чате, MATERIAL_CONFLICT_TYPES). Граф помечает конфликтом и
# пары участников / упоминаний с разными именами — это не противоречие, закрывается правилом без модели.
MATERIAL_TYPES = frozenset({"decision", "task", "fact"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _value(ent: Entity) -> dict[str, Any]:
    try:
        value = json.loads(ent.value_json or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def link_review(link: Link) -> dict[str, Any] | None:
    try:
        review = json.loads(link.model_review) if link.model_review else None
    except json.JSONDecodeError:
        return None
    return review if isinstance(review, dict) else None


def _order_key(ent: Entity, art: Artifact, value: dict[str, Any]) -> tuple:
    from app.app.services.project_deadlines import artifact_reference_date

    return (artifact_reference_date(art) or date.min, float(value.get("start_sec") or 0.0), art.id, ent.id)


def _source(settings: Settings, ent: Entity, art: Artifact, value: dict[str, Any], cache: dict) -> list[str]:
    from app.app.services.review_requirements import _call_source, _document_source, _statement

    text = _statement(ent.entity_type, value)
    if art.file_type == "audio":
        if art.id not in cache:
            from app.app.services.transcript_turns import load_turns

            cache[art.id] = load_turns(settings, art)
        return _call_source(value, text, cache[art.id])[0]
    if art.id not in cache:
        from app.app.services.summarize_document import load_document_source_text

        cache[art.id] = load_document_source_text(settings, art)
    return _document_source(text, cache[art.id])


def pending_links(db: Session, artifact: Artifact | None = None, *, project_id: int | None = None) -> list[Link]:
    """Неразрешённые конфликты артефакта (или проекта), у которых нет вердикта модели текущей версии промпта."""
    q = select(Link).where(Link.link_type == CONFLICT)
    if artifact is not None:
        ids = select(Entity.id).where(Entity.artifact_id == artifact.id)
        q = q.where(Link.project_id == artifact.project_id, or_(Link.source_entity_id.in_(ids), Link.target_entity_id.in_(ids)))
    elif project_id is not None:
        q = q.where(Link.project_id == project_id)
    out = []
    for link in db.scalars(q.order_by(Link.id)).all():
        if db.scalar(select(Resolution.id).where(Resolution.link_id == link.id)):
            continue
        review = link_review(link)
        if review and review.get("prompt_version") == PROMPT_VERSION and not review.get("undone"):
            continue
        out.append(link)
    return out


def _pair(db: Session, settings: Settings, link: Link, cache: dict) -> dict[str, Any] | None:
    a, b = db.get(Entity, link.source_entity_id), db.get(Entity, link.target_entity_id)
    if a is None or b is None or not a.is_active or not b.is_active:
        return None
    arts = {a.artifact_id: db.get(Artifact, a.artifact_id), b.artifact_id: db.get(Artifact, b.artifact_id)}
    va, vb = _value(a), _value(b)
    older, newer = sorted([(a, va), (b, vb)], key=lambda row: _order_key(row[0], arts[row[0].artifact_id], row[1]))
    from app.app.services.retrieval import format_entity_line

    from app.app.services.conflict_rules import source_weight
    from app.app.services.review_requirements import _statement

    def side(ent: Entity, value: dict[str, Any]) -> dict[str, Any]:
        art = arts[ent.artifact_id]
        text = _statement(ent.entity_type, value)
        return {"id": ent.id, "artifact_id": ent.artifact_id, "line": format_entity_line(ent.entity_type, ent.value_json, art),
                "text": text, "weight": source_weight(ent.entity_type, value, art, text),
                "source": _source(settings, ent, art, value, cache), "human": bool(value.get("human_review"))}

    material = a.entity_type in MATERIAL_TYPES and b.entity_type in MATERIAL_TYPES
    return {"link_id": link.id, "project_id": link.project_id, "older": side(*older), "newer": side(*newer),
            "material": material, "types": sorted({a.entity_type, b.entity_type})}


def _user_prompt(pair: dict[str, Any]) -> str:
    from app.app.services.conflict_rules import WEIGHT_LABELS

    def block(tag: str, side: dict[str, Any]) -> str:
        src = "\n".join(side["source"]) or "(источник не найден)"
        return f"Пункт {tag}: {side['line']}\nВес источника: {WEIGHT_LABELS.get(side['weight'], side['weight'])}\nИсточник:\n{src}"

    return block("A (раньше)", pair["older"]) + "\n\n" + block("B (позже)", pair["newer"])


def interpret(pair: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Ответ модели (отношение пунктов) → вердикт по правилам Р1: ``verdict``, ``reason``, ``rule``, ``reject_kind``.

    Модель не выбирает победителя: ``replaces``/``not_decision``/``misextracted`` указывают на пункт, ``contradicts`` —
    победителя даёт вес источника, затем дата. Совместимый ответ старого формата (``verdict``) принимается как есть.
    """
    from app.app.services.conflict_rules import cancel_allowed, winner_by_weight

    reason = str(data.get("reason") or "")
    relation = str(data.get("relation") or "").strip().lower()
    which = str(data.get("which") or "").strip().upper()
    if not relation:
        verdict = str(data.get("verdict") or "").strip().lower()
        return {"verdict": verdict if verdict in VERDICTS else "unsure", "reason": reason, "rule": None, "reject_kind": "superseded",
                "relation": None}
    out = {"verdict": "unsure", "reason": reason, "rule": None, "reject_kind": "superseded", "relation": relation}
    older, newer = pair["older"], pair["newer"]
    pointed = {"A": ("older", older, newer), "B": ("newer", newer, older)}.get(which)
    if relation in ("refines", "same", "different_objects"):
        out.update(verdict="not_conflict", rule=f"relation:{relation}")
    elif relation == "contradicts":
        verdict, rule = winner_by_weight(older, newer)
        out.update(verdict=verdict, rule=rule)
    elif relation == "replaces" and pointed:
        role, canceller, cancelled = pointed
        if role == "older":
            out["reason"] = f"раннее не может отменить позднее; модель: {reason}"
        elif not cancel_allowed(canceller, cancelled):
            out["reason"] = f"отмена из более слабого источника; модель: {reason}"
        else:
            out.update(verdict="newer_wins", rule="rule:explicit_cancel")
    elif relation == "not_decision" and pointed:
        role, _weak, _strong = pointed
        out.update(verdict="newer_wins" if role == "older" else "older_wins", rule="rule:not_a_decision", reject_kind="not_decision")
    elif relation == "misextracted" and pointed:
        role, wrong, _right = pointed
        # Вторая опора, кроме слов модели: источник пункта его и правда не подтверждает.
        if not _source_supports(wrong):
            out.update(verdict="newer_wins" if role == "older" else "older_wins", rule="rule:misextracted", reject_kind="not_said")
        else:
            out["reason"] = f"модель считает пункт извлечённым неверно, но источник его подтверждает; модель: {reason}"
    return out


def _ask(settings: Settings, system: str, user: str) -> dict[str, Any]:
    from app.app.services.llm import chat_completion_no_think, strip_json_fence

    prompt = user
    last = ""
    for attempt in range(2):
        reply = chat_completion_no_think(settings, system=system, user=prompt, max_tokens=512)
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


SUPPORT_MIN_COVERAGE = 0.3


def _source_supports(side: dict[str, Any]) -> bool:
    """Подтверждает ли источник текст пункта: доля основ слов пункта, найденных в источнике."""
    from app.app.services.contract_text import coverage, stems

    need = stems(side.get("text") or "")
    if not need or not side.get("source"):
        return False
    return coverage(need, stems(" ".join(side["source"]))) >= SUPPORT_MIN_COVERAGE


def rule_before_model(pair: dict[str, Any]) -> dict[str, Any] | None:
    """Явная отмена без модели: фраза отмены в источнике более нового пункта и отменяющий не слабее отменяемого (Р1).
    Общая для проверки конфликтов и ``graph._try_auto_resolve``."""
    from app.app.services.conflict_rules import cancel_allowed, cancel_phrase

    phrase = cancel_phrase(pair["newer"]["source"])
    if not phrase or not cancel_allowed(pair["newer"], pair["older"]):
        return None
    return {"verdict": "newer_wins", "reason": f"явная отмена в источнике более нового пункта: «{phrase}»",
            "rule": "rule:explicit_cancel", "reject_kind": "superseded", "relation": "replaces"}


def ask_model(settings: Settings, system: str, pair: dict[str, Any]) -> tuple[dict[str, Any], float]:
    """Спросить модель об отношении пунктов и перевести ответ в вердикт по правилам. Бросает, если ответ не разобран."""
    data = _ask(settings, system, _user_prompt(pair))
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return interpret(pair, data), confidence


def _author(model: str, rule: str | None) -> str:
    """Кто решил — для строки причины: правило (без модели) или модель (с правилом, если победителя дал код)."""
    if model.startswith("rule"):
        return f"правило {model.split(':', 1)[1]}" if ":" in model else "правило"
    return f"модель ({rule})" if rule else "модель"


def apply_conflict_verdict(db: Session, settings: Settings, pair: dict[str, Any], *, verdict: str, reason: str,
                           confidence: float, mode: str, model: str, rule: str | None = None,
                           reject_kind: str = "superseded", relation: str | None = None) -> str:
    """Записать вердикт по конфликту. Вернуть итоговый вердикт (может стать unsure, если мешает решение человека)."""
    from app.app.services.conflict_rules import is_material_topic

    link = db.get(Link, pair["link_id"])
    if link is None or db.scalar(select(Resolution.id).where(Resolution.link_id == link.id)):
        return "skipped"
    if verdict not in VERDICTS or confidence < MIN_CONFIDENCE:
        verdict = "unsure"
    older, newer = pair["older"], pair["newer"]
    winner, loser = (newer, older) if verdict == "newer_wins" else (older, newer)
    if verdict in ("newer_wins", "older_wins") and loser["human"]:
        verdict, reason = "unsure", f"проигравшую сторону подтвердил человек; модель: {reason}"
    elif verdict in ("newer_wins", "older_wins") and not _source_supports(winner):
        # Замер 2026-09-24: модель «объяснила» неверно извлечённый факт («24 слайда» из «видеокарта 24 ГБ») и отдала ему
        # победу с уверенностью 0.95. Выключать верное решение ради пункта без опоры в источнике нельзя.
        verdict, reason = "unsure", f"источник победившего пункта его не подтверждает; модель: {reason}"
    wins = verdict in ("newer_wins", "older_wins")
    review: dict[str, Any] = {"verdict": verdict, "reason": (reason or "")[:300], "confidence": round(float(confidence), 3),
                              "mode": mode, "model": model, "prompt_version": PROMPT_VERSION, "at": _now(),
                              "older": older["id"], "newer": newer["id"], "rule": rule, "relation": relation,
                              "reject_kind": reject_kind if wins else None,
                              "important": wins and is_material_topic(older.get("text") or "", newer.get("text") or "")}
    author = _author(model, rule)
    if mode == MODE_LIVE and wins:
        db.add(Resolution(link_id=link.id, winning_entity_id=winner["id"], losing_entity_id=loser["id"],
                          reason=f"{author}: {reason}"[:1000], resolved_by=None, status="model_resolved"))
        lose_ent = db.get(Entity, loser["id"])
        previous = _value(lose_ent).get("model_review")
        record_model_review(lose_ent, settings, verdict="reject", reason=f"заменено: {reason}", confidence=confidence,
                            mode=MODE_LIVE, model=model, prompt_version=PROMPT_VERSION,
                            extra={"reject_kind": reject_kind, "conflict_link": link.id, "winner": winner["id"], "previous": previous})
        review["resolution"], review["deactivated"] = "model_resolved", loser["id"]
    elif mode == MODE_LIVE and verdict == "not_conflict":
        db.add(Resolution(link_id=link.id, winning_entity_id=newer["id"], losing_entity_id=older["id"],
                          reason=f"{author}: не конфликт — {reason}"[:1000], resolved_by=None, status="model_dismissed"))
        review["resolution"] = "model_dismissed"
    link.model_review = json.dumps(review, ensure_ascii=False)
    log_event(logger, "review.conflict", link=link.id, verdict=verdict, confidence=review["confidence"], mode=mode,
              rule=rule, important=review["important"])
    return verdict


def effective_mode(project: Any, mode: str) -> str:
    """Режим проверки конфликтов проекта: автооткат (``conflict_review_mode=shadow``) понижает live до тени."""
    try:
        forced = str((project.settings() or {}).get(PROJECT_MODE_KEY) or "").strip().lower()
    except (AttributeError, ValueError, TypeError):
        forced = ""
    return MODE_SHADOW if mode == MODE_LIVE and forced == MODE_SHADOW else mode


def undo_rate(db: Session, project_id: int, *, now: datetime | None = None) -> tuple[int, int]:
    """(авторешений live с победителем за окно, из них отменено человеком)."""
    now = now or datetime.now(timezone.utc)
    total = undone = 0
    for link in db.scalars(select(Link).where(Link.project_id == project_id, Link.link_type == CONFLICT)).all():
        review = link_review(link)
        if not review or review.get("mode") != MODE_LIVE or review.get("verdict") not in ("newer_wins", "older_wins"):
            continue
        try:
            at = datetime.fromisoformat(str(review.get("at")))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if (now - at).days > UNDO_WINDOW_DAYS:
            continue
        total += 1
        undone += bool(review.get("undone"))
    return total, undone


def check_undo_rate(db: Session, project_id: int) -> bool:
    """После «Вернуть»: если человек отменяет больше UNDO_MAX_RATE авторешений — конфликты проекта уходят в тень."""
    from app.app.models.project import Project

    total, undone = undo_rate(db, project_id)
    if total < UNDO_MIN_DECISIONS or undone / total <= UNDO_MAX_RATE:
        return False
    project = db.get(Project, project_id)
    if project is None:
        return False
    data = project.settings() or {}
    if data.get(PROJECT_MODE_KEY) == MODE_SHADOW:
        return False
    data[PROJECT_MODE_KEY] = MODE_SHADOW
    project.set_settings(data)
    log_event(logger, "review.conflicts.rollback", project=project_id, decisions=total, undone=undone)
    return True


def release_conflict(db: Session, link_id: int, *, user_name: str) -> bool:
    """Снять разрешение модели по связи (если есть) и пометить вердикт отменённым. Сущность возвращает вызывающий."""
    link = db.get(Link, link_id)
    if link is None:
        return False
    res = db.scalar(select(Resolution).where(Resolution.link_id == link_id, Resolution.status.in_(MODEL_STATUSES)))
    if res is not None:
        db.delete(res)
    review = link_review(link)
    if review is not None and not review.get("undone"):
        review["undone"] = {"by": user_name, "at": _now()}
        link.model_review = json.dumps(review, ensure_ascii=False)
    return res is not None


def undo_conflict(db: Session, link_id: int, *, user_name: str, user_id: int | None) -> dict[str, Any]:
    """«Вернуть» решение модели по конфликту: снять разрешение, включить выключенную моделью сторону."""
    link = db.get(Link, link_id)
    review = link_review(link) if link else None
    if link is None or review is None or review.get("undone") or review.get("mode") != MODE_LIVE:
        raise ValueError("у конфликта нет действующего решения модели")
    release_conflict(db, link_id, user_name=user_name)
    restored = None
    if review.get("deactivated"):
        ent = db.get(Entity, review["deactivated"])
        mr = _value(ent).get("model_review") if ent else None
        if ent is not None and isinstance(mr, dict) and mr.get("conflict_link") == link_id and not mr.get("undone"):
            undo_model_review(ent, user_name=user_name, user_id=user_id)
            restored = ent.id
    db.flush()
    rolled_back = check_undo_rate(db, link.project_id)
    return {"link_id": link_id, "restored_entity_id": restored, "rolled_back_to_shadow": rolled_back}


def _promote_shadow(db: Session, settings: Settings, artifact: Artifact, cache: dict) -> int:
    """Режим live: применить теневые вердикты той же версии промпта без повторного вызова модели."""
    ids = select(Entity.id).where(Entity.artifact_id == artifact.id)
    links = db.scalars(
        select(Link).where(Link.project_id == artifact.project_id, Link.link_type == CONFLICT,
                           or_(Link.source_entity_id.in_(ids), Link.target_entity_id.in_(ids)))
    ).all()
    promoted = 0
    for link in links:
        review = link_review(link)
        if (not review or review.get("mode") != "shadow" or review.get("prompt_version") != PROMPT_VERSION
                or review.get("undone") or db.scalar(select(Resolution.id).where(Resolution.link_id == link.id))):
            continue
        pair = _pair(db, settings, link, cache)
        if pair is None:
            continue
        apply_conflict_verdict(db, settings, pair, verdict=review.get("verdict") or "unsure", reason=review.get("reason") or "",
                               confidence=float(review.get("confidence") or 0.0), mode=MODE_LIVE, model=review.get("model") or "",
                               rule=review.get("rule"), reject_kind=review.get("reject_kind") or "superseded",
                               relation=review.get("relation"))
        promoted += 1
    return promoted


def review_artifact_conflicts(db: Session, settings: Settings, artifact: Artifact, mode: str) -> dict[str, Any]:
    """Проверить неразрешённые конфликты, в которых участвует артефакт."""
    from app.app.services.prompts import load_prompt_block
    from app.app.services.review_requirements import _model_label

    started = time.monotonic()
    stats = {"total": 0, "newer_wins": 0, "older_wins": 0, "not_conflict": 0, "unsure": 0, "skipped": 0, "llm_calls": 0, "llm_errors": 0, "promoted": 0}
    artifact_key = artifact.artifact_id
    cache: dict = {}
    mode = effective_mode(artifact.project, mode)
    if mode == MODE_LIVE:
        stats["promoted"] = _promote_shadow(db, settings, artifact, cache)
        db.commit()
    pairs = [p for p in (_pair(db, settings, link, cache) for link in pending_links(db, artifact)) if p]
    stats["total"] = len(pairs)
    stats["rule_not_material"] = 0
    for pair in [p for p in pairs if not p["material"]]:
        verdict = apply_conflict_verdict(
            db, settings, pair, verdict="not_conflict", confidence=1.0, mode=mode, model="rule",
            reason=f"{' / '.join(pair['types'])}: разные значения — не противоречие (противоречат только решения, задачи, факты)",
        )
        stats[verdict] = stats.get(verdict, 0) + 1
        stats["rule_not_material"] += 1
    db.commit()
    pairs = [p for p in pairs if p["material"]]
    stats["rule_explicit_cancel"] = 0
    for pair in pairs:
        decided = rule_before_model(pair)
        if decided is None:
            continue
        verdict = apply_conflict_verdict(db, settings, pair, verdict=decided["verdict"], reason=decided["reason"], confidence=1.0,
                                         mode=mode, model=decided["rule"], rule=decided["rule"], relation=decided["relation"])
        stats[verdict] = stats.get(verdict, 0) + 1
        stats["rule_explicit_cancel"] += 1
        pair["decided"] = True
    db.commit()
    pairs = [p for p in pairs if not p.get("decided")]
    if not pairs:
        return {**stats, "seconds": round(time.monotonic() - started, 1)}
    system = load_prompt_block(PROMPT_PATH)
    model = _model_label(settings)
    db.commit()
    if not settings.dry_run:
        from app.app.services.gpu_phase import prepare_for_llm

        prepare_for_llm(settings.lmstudio_model, settings=settings)
    for pair in pairs:
        db.commit()  # DECISIONS 2026-09-21: соединение БД не держим на время ответа LLM
        try:
            decided, confidence = ask_model(settings, system, pair)
        except Exception as exc:  # noqa: BLE001 — конфликт останется без вердикта, следующий прогон повторит
            stats["llm_errors"] += 1
            log_event(logger, "review.llm_failed", artifact=artifact_key, link=pair["link_id"], error=str(exc)[:300])
            continue
        stats["llm_calls"] += 1
        verdict = apply_conflict_verdict(db, settings, pair, verdict=decided["verdict"], reason=decided["reason"],
                                         confidence=confidence, mode=mode, model=model, rule=decided["rule"],
                                         reject_kind=decided["reject_kind"], relation=decided["relation"])
        stats[verdict] = stats.get(verdict, 0) + 1
        db.commit()
    stats["seconds"] = round(time.monotonic() - started, 1)
    log_event(logger, "review.conflicts", artifact=artifact_key, mode=mode, **stats)
    return stats


def conflict_rows(db: Session, project_id: int, *, view: str = "pending") -> list[dict[str, Any]]:
    """Конфликты для экрана: pending — ждут человека (с теневым/неуверенным вердиктом модели); model — решены моделью;
    important — решённые моделью или правилом по деньгам/срокам и не отменённые (сводка важного, Р3)."""
    from app.app.services.retrieval import format_entity_line

    links = db.scalars(select(Link).where(Link.project_id == project_id, Link.link_type == CONFLICT).order_by(Link.id)).all()
    out = []
    for link in links:
        res = db.scalar(select(Resolution).where(Resolution.link_id == link.id))
        review = link_review(link)
        by_model = res is not None and res.status in MODEL_STATUSES
        if view == "pending" and res is not None:
            continue
        if view == "model" and not by_model:
            continue
        if view == "important" and not (by_model and review and review.get("important") and not review.get("undone")):
            continue
        side = {}
        for key, ent_id in (("source", link.source_entity_id), ("target", link.target_entity_id)):
            ent = db.get(Entity, ent_id)
            art = db.get(Artifact, ent.artifact_id) if ent else None
            side[key] = {"id": ent_id, "type": ent.entity_type if ent else None, "value": ent.value_json if ent else None,
                         "active": bool(ent and ent.is_active),
                         "line": format_entity_line(ent.entity_type, ent.value_json, art) if ent else None}
        out.append({"link_id": link.id, "score": link.similarity_score, **side, "model_review": review,
                    "resolution": {"status": res.status, "reason": res.reason, "winner": res.winning_entity_id} if res else None})
    return out
