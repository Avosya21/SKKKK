"""Журнал решений в ответах чата (docs/plans/DECISION-MEMORY-PLAN.md, Э6): «что решили по X», «что отменили», «что было верно на дату».

К вопросу подбираются предметы памяти решений (по близости вектора вопроса к центру предмета и по словам названия) и в подсказку
модели добавляется их лента: состояние и с какого момента, итог словами, реплики с датой, стороной, автором и цитатой. Если в вопросе
названа дата, добавляется состояние предмета на этот день. Чат журнал не меняет (DECISIONS): он только читает.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.decision_memory import STATE_LABELS, Subject
from app.app.models.speech_act import ACT_LABELS, SpeechAct

MAX_SUBJECTS = 3
MIN_VECTOR_SCORE = 0.45
MIN_WORDS_SCORE = 0.5
MAX_ACTS_SHOWN = 8
BLOCK_CHARS = 4200
_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
SIDE_RU = {"customer": "заказчик", "executor": "исполнитель"}


def date_in_question(question: str, *, today: date | None = None) -> str | None:
    """Дата из вопроса: 15.09.2026, 2026-09-15, «15 сентября 2026» или «15 сентября» (год — текущий, а если дата позже сегодня — прошлый)."""
    q = question or ""
    today = today or date.today()
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", q)
    if m:
        return _safe(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", q)
    if m:
        return _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")(?:\s+(\d{4}))?", q.lower())
    if m:
        month, day = _MONTHS[m.group(2)], int(m.group(1))
        year = int(m.group(3)) if m.group(3) else today.year
        found = _safe(year, month, day)
        if found and not m.group(3) and found > today.isoformat():
            found = _safe(year - 1, month, day)
        return found
    return None


def _safe(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _words_score(question: str, title: str) -> float:
    from app.app.services import contract_text as ct

    need = ct.stems(title)
    return ct.coverage(need, ct.stems(question)) if need else 0.0


def pick_subjects(db: Session, project_id: int, q_vec: list[float] | None, question: str, *, limit: int = MAX_SUBJECTS) -> list[Subject]:
    from app.app.services.subjects import cosine, unpack

    scored: list[tuple[float, Subject]] = []
    for s in db.scalars(select(Subject).where(Subject.project_id == project_id, Subject.is_active.is_(True))).all():
        vec = cosine(q_vec or [], unpack(s.embedding)) if q_vec else 0.0
        words = _words_score(question, s.title)
        if vec >= MIN_VECTOR_SCORE or words >= MIN_WORDS_SCORE:
            scored.append((max(vec, words), s))
    scored.sort(key=lambda t: -t[0])
    return [s for _score, s in scored[:limit]]


def _line(a: SpeechAct, days: dict[int, str]) -> str:
    when = (a.said_at or days.get(a.artifact_id, "")).replace("T", " ")[:16]
    side = f" [{SIDE_RU[a.side]}]" if a.side in SIDE_RU else ""
    return f"- {when}{side} {a.author or '?'} — {ACT_LABELS.get(a.act, a.act)}: «{a.quote[:240]}»"


def decision_block(db: Session, project_id: int, q_vec: list[float] | None, question: str, *, today: date | None = None) -> str:
    """Текст для подсказки модели или пустая строка, если подходящих предметов нет."""
    from app.app.models.artifact import Artifact
    from app.app.services import decision_log as dl
    from app.app.services.subjects import _day_of

    picked = pick_subjects(db, project_id, q_vec, question)
    if not picked:
        return ""
    when = date_in_question(question, today=today)
    days = {a.id: _day_of(a) for a in db.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()}
    parts: list[str] = []
    for s in picked:
        acts = list(db.scalars(select(SpeechAct).where(SpeechAct.subject_id == s.id).order_by(SpeechAct.said_at, SpeechAct.artifact_id, SpeechAct.unit_key)).all())
        shown = acts[:2] + acts[-(MAX_ACTS_SHOWN - 2):] if len(acts) > MAX_ACTS_SHOWN else acts
        head = f"Предмет: {s.title}\nСостояние сейчас: {STATE_LABELS.get(s.state, s.state)}" + (f" (с {s.state_since[:16].replace('T', ' ')})" if s.state_since else "")
        if s.summary:
            head += f"\nИтог: {s.summary}"
        if when:
            at = dl.state_at(db, s.id, when)
            head += f"\nСостояние на {when}: {STATE_LABELS.get(at.state, at.state)}" + (" (порядок реплик одного дня неизвестен)" if "ambiguous_order" in at.flags else "")
        parts.append(head + "\nЛента:\n" + "\n".join(_line(a, days) for a in shown))
    text = "\n\n".join(parts)
    return text[:BLOCK_CHARS]
