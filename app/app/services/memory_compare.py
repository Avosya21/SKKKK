"""Сравнение памяти решений с прежним контуром актуальности пунктов (docs/plans/DECISION-MEMORY-STAGES-6-8.md, Э7).

Для каждого пункта проекта (решение или задача из переписки/созвона) берётся состояние по прежнему контуру (``memory_scorecard._state``:
current/done/superseded/duplicate/rejected) и состояние по памяти решений (позиция предмета, в основаниях которой акт пункта, иначе
предмет целиком). Совпавшие по группе «действует / не действует» пишутся как ``agree``; спорные идут арбитру — модели, которая читает
ленту реплик и решает, что верно (методы названы «A» и «B» вслепую, порядок чередуется). Человек ничего не размечает: вердикты
арбитра дают долю «права память» для ``memory_mode.readiness``. Состояние прежнего контура и пунктов не меняется.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.decision_memory import POSITION_LABELS, STATE_LABELS, MemoryShadowReview, Subject, SubjectPosition
from app.app.models.entity import Entity
from app.app.models.speech_act import ACT_LABELS, SpeechAct

logger = logging.getLogger("skkkk.memory_compare")

PROMPT_VERSION = "memory_arbiter-v2"
ACTIVE = {"current", "done"}
SUBJECT_TO_OLD = {"open": "current", "proposed": "current", "agreed": "current", "implemented": "done", "rejected": "rejected",
                  "withdrawn": "rejected", "superseded": "superseded"}
TURN_TOLERANCE = 0.02
MAX_FEED = 12
MIN_POSITION_MATCH = 0.34  # доля слов пункта в названии позиции, чтобы выбрать позицию по смыслу, а не по основанию
ENTITY_TYPES = ("decision", "task")


def _system() -> str:
    from app.app.services.prompts import load_prompt_block
    from app.app.services.speech_acts import ROOT

    return load_prompt_block(ROOT / "docs" / "prompts" / "memory_arbiter.md")


def _view_system() -> str:
    from app.app.services.prompts import load_prompt_block
    from app.app.services.speech_acts import ROOT

    return load_prompt_block(ROOT / "docs" / "prompts" / "memory_view.md")


def _ask_default(settings: Settings) -> Callable[[str, str], str]:
    from app.app.services.llm import chat_completion_no_think

    def ask(system: str, user: str) -> str:
        return chat_completion_no_think(settings, system=system, user=user, max_tokens=300)

    return ask


_WORD = re.compile(r"[а-яёa-z0-9]{3,}", re.IGNORECASE)
_NUM_GAP = re.compile(r"(?<=\d)[\s ](?=\d{3})")


def stems(text: str) -> set[str]:
    """Основы слов для сопоставления пункта с названием позиции; в отличие от ``contract_text.stems`` учитывает числа («126 000» — одно слово)."""
    return {w.lower()[:6] for w in _WORD.findall(_NUM_GAP.sub("", text or ""))}


def coverage(needle: set[str], hay: set[str]) -> float:
    return len(needle & hay) / len(needle) if needle else 0.0


def group(state: str) -> str:
    return "active" if state in ACTIVE else "inactive"


def _acts_for(acts: list[SpeechAct], artifact_id: int, value: dict[str, Any]) -> list[SpeechAct]:
    """Акты на том же адресе, что и пункт прежнего контура: сообщение переписки или реплика созвона (по началу в секундах)."""
    if value.get("message_id") is not None:
        ref = f"M{int(value['message_id'])}"
        return [a for a in acts if a.artifact_id == artifact_id and a.unit_ref == ref]
    if value.get("start_sec") is not None:
        start = float(value["start_sec"])
        return [a for a in acts if a.artifact_id == artifact_id and a.start_sec is not None and abs(float(a.start_sec) - start) <= TURN_TOLERANCE]
    return []


def _pick(db: Session, text: str, candidates: list[SpeechAct], subjects: dict[int, Subject], positions: dict[int, list[SubjectPosition]]) -> tuple[SpeechAct, Subject, str, str] | None:
    """Из актов реплики-источника выбирает тот, что ближе к тексту пункта (в одной реплике бывает отказ и новое предложение):
    возвращает (акт, предмет, состояние по памяти, пояснение)."""
    need = stems(text)
    best: tuple[float, SpeechAct, Subject, str, str] | None = None
    for act in candidates:
        subject = subjects.get(act.subject_id)
        if subject is None:
            continue
        state, why = new_state(db, act, subject, positions, text)
        label = next((p.label for p in positions.get(subject.id, []) if act.id in json.loads(p.basis_json or "[]")), "")
        score = coverage(need, stems(f"{label} {act.quote} {act.about or ''}"))
        if best is None or score > best[0]:
            best = (score, act, subject, state, why)
    return best[1:] if best else None


def new_state(db: Session, act: SpeechAct, subject: Subject, positions: dict[int, list[SubjectPosition]], text: str = "") -> tuple[str, str]:
    """Состояние пункта по памяти: позиция предмета, ближайшая по словам к тексту пункта (иначе та, в основаниях которой акт),
    иначе предмет целиком. Возвращает (состояние, пояснение)."""
    mine = positions.get(subject.id, [])
    chosen = None
    if mine and text:
        need = stems(text)
        scored = sorted(((coverage(need, stems(p.label)), p) for p in mine), key=lambda t: -t[0])
        if scored and scored[0][0] >= MIN_POSITION_MATCH:
            chosen = scored[0][1]
    if chosen is None:
        for pos in mine:
            try:
                basis = json.loads(pos.basis_json or "[]")
            except ValueError:
                basis = []
            if act.id in basis:
                chosen = pos
                break
    if chosen is not None:
        mapped = {"current": "current", "open": "current", "rejected": "rejected", "superseded": "superseded"}[chosen.state]
        return mapped, f"позиция «{chosen.label}»: {POSITION_LABELS.get(chosen.state, chosen.state)}" + (f" (заменена: {chosen.replaced_by})" if chosen.replaced_by else "")
    return SUBJECT_TO_OLD.get(subject.state, "current"), f"предмет «{subject.title}»: {STATE_LABELS.get(subject.state, subject.state)}. {subject.summary or ''}".strip()


def _feed(acts: list[SpeechAct]) -> str:
    shown = acts if len(acts) <= MAX_FEED else acts[:2] + acts[-(MAX_FEED - 2):]
    lines = []
    for a in shown:
        side = {"customer": "заказчик", "executor": "исполнитель"}.get(a.side or "", "")
        lines.append(f"- {(a.said_at or '')[:16].replace('T', ' ')} {('[' + side + '] ') if side else ''}{a.author or '?'} — {ACT_LABELS.get(a.act, a.act)}: «{a.quote[:220]}»")
    return "\n".join(lines)


def parse_truth(raw: str) -> tuple[str, str]:
    from app.app.services.llm import strip_json_fence

    data = json.loads(strip_json_fence(raw))
    truth = str(data.get("truth") or "").strip().lower() if isinstance(data, dict) else ""
    if truth not in ("active", "inactive", "unclear"):
        raise ValueError(f"truth не из списка: {truth!r}")
    return truth, str(data.get("reason") or "").strip()[:300]


def compare_project(db: Session, settings: Settings, project_id: int, *, ask: Callable[[str, str], str] | None = None) -> dict[str, int]:
    """Пересобрать сравнение проекта. Старые строки ``memory_shadow_review`` заменяются."""
    from app.app.services.memory_scorecard import _state

    ask = ask or _ask_default(settings)
    system = _system()
    acts = list(db.scalars(select(SpeechAct).where(SpeechAct.project_id == project_id, SpeechAct.subject_id.is_not(None))).all())
    subjects = {s.id: s for s in db.scalars(select(Subject).where(Subject.project_id == project_id, Subject.is_active.is_(True))).all()}
    positions: dict[int, list[SubjectPosition]] = {}
    for pos in db.scalars(select(SubjectPosition).where(SubjectPosition.subject_id.in_(list(subjects) or [-1]))).all():
        positions.setdefault(pos.subject_id, []).append(pos)
    by_subject: dict[int, list[SpeechAct]] = {}
    for a in sorted(acts, key=lambda x: (x.said_at or "", x.artifact_id, x.unit_key)):
        by_subject.setdefault(a.subject_id, []).append(a)
    items = db.execute(select(Entity, Artifact).join(Artifact, Artifact.id == Entity.artifact_id)
                       .where(Artifact.project_id == project_id, Entity.entity_type.in_(ENTITY_TYPES)).order_by(Entity.id)).all()
    stats = {"entities": len(items), "compared": 0, "agree": 0, "disputed": 0, "new_right": 0, "old_right": 0, "neither": 0, "unclear": 0,
             "not_covered": 0, "duplicates": 0, "agree_after_view": 0, "errors": 0}
    rows: list[MemoryShadowReview] = []
    # кандидаты в споры: (пункт, предмет, старое, новое по памяти, пояснение памяти, текст пункта, лента, основание прежнего контура) — всё готовим до вызовов модели
    candidates: list[tuple[int, int, str, str, str, str, str, str]] = []
    for ent, art in items:
        try:
            value = json.loads(ent.value_json or "{}")
        except ValueError:
            continue
        text = str(value.get("text") or value.get("description") or "")
        picked = _pick(db, text, _acts_for(acts, art.id, value), subjects, positions)
        if picked is None:
            stats["not_covered"] += 1  # документы и пункты без реплики-источника память не покрывает
            continue
        act, subject, new, why = picked
        old = _state(value, bool(ent.is_active))
        if old == "duplicate":
            stats["duplicates"] += 1  # «дубль» — решение несёт другой пункт; о том, действует ли оно, прежний контур здесь не высказывается
            continue
        stats["compared"] += 1
        if group(old) == group(new):
            stats["agree"] += 1
            rows.append(MemoryShadowReview(project_id=project_id, entity_id=ent.id, subject_id=subject.id, old_state=old, new_state=new, verdict="agree", by="model"))
            continue
        rel = value.get("relevance") or {}
        candidates.append((ent.id, subject.id, old, new, why, text, _feed(by_subject.get(subject.id, [act])), str(rel.get("reason") or "")))
    db.commit()
    view_system = _view_system()
    n_disputes = 0
    for entity_id, subject_id, old, new, why, text, feed, old_reason in candidates:
        # шаг 1: что память решений говорит именно об этом пункте (пункт бывает сформулирован как итог — «отказаться от X» при отклонённом X)
        view, view_reason = "unclear", ""
        for attempt in range(2):
            try:
                view, view_reason = parse_truth(ask(view_system, f"Пункт: {text[:500]}\n\nЧто известно по памяти решений: {why}"))
                break
            except (ValueError, json.JSONDecodeError, RuntimeError):
                continue
        if view != "unclear":
            new = "current" if view == "active" else (new if new in ("rejected", "superseded") else "superseded")
            why = f"{why} Для этого пункта: {'действует' if view == 'active' else 'не действует'} — {view_reason}"
        if group(old) == group(new):
            stats["agree"] += 1
            stats["agree_after_view"] += 1
            rows.append(MemoryShadowReview(project_id=project_id, entity_id=entity_id, subject_id=subject_id, old_state=old, new_state=new, verdict="agree", by="model"))
            continue
        # шаг 2: спор — арбитр читает ленту и решает, что верно
        n_disputes += 1
        old_text = old + (f": {old_reason}" if old_reason else "")
        first, second = (old_text, f"{new}: {why}") if n_disputes % 2 == 1 else (f"{new}: {why}", old_text)  # порядок чередуется
        user = "\n".join([f"Пункт: {text[:500]}", "", "Лента реплик по вопросу:", feed, "", f"Метод A: {group_ru(first)}", f"Метод B: {group_ru(second)}"])
        truth, reason = "unclear", ""
        for attempt in range(2):
            try:
                truth, reason = parse_truth(ask(system, user))
                break
            except (ValueError, json.JSONDecodeError, RuntimeError) as exc:
                if attempt == 1:
                    stats["errors"] += 1
                    reason = f"арбитр не ответил: {str(exc)[:120]}"
        stats["disputed"] += 1
        if truth == "unclear":
            verdict = "unclear"
        elif truth == group(old):
            verdict = "old"
        elif truth == group(new):
            verdict = "new"
        else:
            verdict = "neither"
        stats[{"old": "old_right", "new": "new_right"}.get(verdict, verdict)] += 1
        rows.append(MemoryShadowReview(project_id=project_id, entity_id=entity_id, subject_id=subject_id, old_state=old, new_state=new, verdict=verdict, reason=reason or None, by="model"))
    db.execute(delete(MemoryShadowReview).where(MemoryShadowReview.project_id == project_id, MemoryShadowReview.by == "model"))
    db.add_all(rows)
    db.commit()
    log_event(logger, "memory_compare.done", project=project_id, **stats)
    return stats


def group_ru(text: str) -> str:
    """«current: …» -> «действует: …» для арбитра (внутренние названия состояний ему не нужны)."""
    head, _, tail = text.partition(":")
    return ("действует" if group(head.strip()) == "active" else "не действует") + (f" ({tail.strip()[:300]})" if tail.strip() else "")
