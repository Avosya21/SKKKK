"""Предметы обсуждения: каждому речевому акту — предмет, о котором идёт речь (docs/plans/DECISION-MEMORY-PLAN.md, Э3).

Предмет — один вопрос, по которому что-то предлагают, принимают, отклоняют. Акты раскладываются по предметам в порядке времени,
жадно: вектор акта (BGE-M3: «о чём» + цитата) сравнивается с центрами уже созданных предметов.
- близость ≥ ``HIGH`` — акт в этот предмет;
- близость < ``LOW`` — новый предмет;
- между ними — один узкий вопрос модели («тот же предмет или новый», ``docs/prompts/subject_match.md``) по двум-трём кандидатам.
Решение код проверяет: номер кандидата должен быть из предложенных. Центр предмета — среднее векторов его актов; он хранится в
``subjects.embedding`` и нужен для дозагрузки новых актов без пересчёта всего проекта (Э8).
Назначения лежат в ``speech_acts.subject_id``; события журнала строит ``decision_log.rebuild_events_from_acts``.
"""
from __future__ import annotations

import json
import logging
import math
import struct
from typing import Any, Callable

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.decision_memory import DecisionEvent, Subject, SubjectPosition
from app.app.models.speech_act import SpeechAct

logger = logging.getLogger("primaryanalitics.subjects")

PROMPT_VERSION = "subject_match-v2"
HIGH = 0.80  # выше — тот же предмет без вопроса модели
LOW = 0.58  # ниже — новый предмет без вопроса модели
CANDIDATES = 3
REACTIVE = {"agreed", "rejected", "deferred", "answered", "done", "cancelled", "blocked"}  # реакции: чаще всего про предмет только что сказанного
RECENT_SUBJECTS = 3  # сколько недавних предметов того же материала добавляется кандидатами для реакций
RECENT_ACTS = 2  # реплик предмета, которые показываются модели
TITLE_MAX = 100
EMBED_CHARS = 320


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def act_text(act: SpeechAct) -> str:
    about = (act.about or "").strip()
    return (f"{about} | {act.quote}" if about else act.quote)[:EMBED_CHARS]


def _day_of(artifact: Artifact | None) -> str:
    from app.app.services.recorded_at import material_date

    day = material_date(artifact)[0] if artifact is not None else None
    return day.isoformat() if day else "0000-00-00"


def _sort_key(act: SpeechAct, days: dict[int, str]) -> tuple[str, int, int]:
    return (act.said_at or days.get(act.artifact_id, "0000-00-00"), act.artifact_id, act.unit_key)


class _Subj:
    """Рабочая копия предмета во время раскладки: центр, число актов, последние акты для вопроса модели."""

    def __init__(self, row: Subject, centroid: list[float], count: int):
        self.row, self.centroid, self.count = row, centroid, count
        self.recent: list[str] = []

    def add(self, vec: list[float], act: SpeechAct) -> None:
        n = self.count
        self.centroid = [(c * n + v) / (n + 1) for c, v in zip(self.centroid, vec)] if self.centroid else list(vec)
        self.count += 1
        self.recent = (self.recent + [f"{act.author or '?'}: {act.quote}"])[-RECENT_ACTS:]


def _system() -> str:
    from app.app.services.prompts import load_prompt_block
    from app.app.services.speech_acts import ROOT

    return load_prompt_block(ROOT / "docs" / "prompts" / "subject_match.md")


def _ask_default(settings: Settings) -> Callable[[str, str], str]:
    from app.app.services.llm import chat_completion_no_think

    def ask(system: str, user: str) -> str:
        return chat_completion_no_think(settings, system=system, user=user, max_tokens=256)

    return ask


def _build_question(act: SpeechAct, cands: list[_Subj]) -> str:
    lines = [f"Новая реплика ({act.author or '?'}): {act.quote}", f"О чём: {act.about or '—'}", "", "Кандидаты:"]
    for i, c in enumerate(cands, 1):
        lines.append(f"{i}. {c.row.title}")
        lines.extend(f"   · {r}" for r in c.recent)
    return "\n".join(lines)


def parse_choice(raw: str, count: int) -> tuple[int | None, str | None]:
    """Ответ модели -> (индекс кандидата с нуля или None, название нового). Невалидный номер — «новый предмет»."""
    from app.app.services.llm import strip_json_fence

    data = json.loads(strip_json_fence(raw))
    same = data.get("same") if isinstance(data, dict) else None
    title = str(data.get("title") or "").strip()[:TITLE_MAX] if isinstance(data, dict) else ""
    if isinstance(same, int) and not isinstance(same, bool) and 1 <= same <= count:
        return same - 1, None
    if isinstance(same, str) and same.strip().isdigit() and 1 <= int(same.strip()) <= count:
        return int(same.strip()) - 1, None
    return None, title or None


def assign(db: Session, settings: Settings, project_id: int, *, rebuild: bool = False,
           embed: Callable[[list[str]], list[list[float]]] | None = None, ask: Callable[[str, str], str] | None = None) -> dict[str, Any]:
    """Назначить предметы актам проекта. ``rebuild`` — всё заново (предметы без решений человека удаляются); иначе только акты без
    предмета, они присоединяются к существующим предметам или создают новые."""
    from app.app.services.ollama_client import embed_texts

    embed = embed or (lambda texts: embed_texts(settings, texts))
    ask = ask or _ask_default(settings)
    if rebuild:
        keep = set(db.scalars(select(DecisionEvent.subject_id).where(DecisionEvent.project_id == project_id, DecisionEvent.decided_by == "human")).all())
        drop = [s for s in db.scalars(select(Subject.id).where(Subject.project_id == project_id)).all() if s not in keep]
        if drop:
            db.execute(update(SpeechAct).where(SpeechAct.subject_id.in_(drop)).values(subject_id=None))
            db.execute(delete(DecisionEvent).where(DecisionEvent.subject_id.in_(drop)))
            db.execute(delete(SubjectPosition).where(SubjectPosition.subject_id.in_(drop)))
            db.execute(delete(Subject).where(Subject.id.in_(drop)))
        db.flush()
    subjects: list[_Subj] = []
    for row in db.scalars(select(Subject).where(Subject.project_id == project_id, Subject.is_active.is_(True))).all():
        subjects.append(_Subj(row, unpack(row.embedding), int(db.scalar(select(func.count()).select_from(SpeechAct).where(SpeechAct.subject_id == row.id)) or 0)))
    pending = db.scalars(select(SpeechAct).where(SpeechAct.project_id == project_id, SpeechAct.subject_id.is_(None))).all()
    stats = {"acts": len(pending), "attached": 0, "created": 0, "asked": 0, "ask_errors": 0, "subjects": 0}
    if not pending:
        stats["subjects"] = len(subjects)
        return stats
    days = {a.id: _day_of(a) for a in db.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()}
    pending.sort(key=lambda a: _sort_key(a, days))
    db.commit()  # соединение БД не держим на время ответов моделей
    vectors = embed([act_text(a) for a in pending])
    if len(vectors) != len(pending):
        raise RuntimeError(f"векторов {len(vectors)} на {len(pending)} актов")
    recent_of: dict[int, list[_Subj]] = {}  # недавние предметы по материалам
    for act, vec in zip(pending, vectors):
        ranked = sorted(((cosine(vec, s.centroid), s) for s in subjects), key=lambda t: -t[0])
        target: _Subj | None = None
        title = (act.about or act.quote)[:TITLE_MAX]
        if ranked and ranked[0][0] >= HIGH:
            target = ranked[0][1]
        elif (ranked and ranked[0][0] >= LOW) or (act.act in REACTIVE and recent_of.get(act.artifact_id)):
            cands = [s for _sim, s in ranked[:CANDIDATES] if _sim >= LOW]
            if act.act in REACTIVE:
                cands += [s for s in reversed(recent_of.get(act.artifact_id, [])) if s not in cands][:RECENT_SUBJECTS]
            stats["asked"] += 1
            try:
                question = _build_question(act, cands)
                db.commit()  # соединение БД не держим на время ответа модели
                idx, new_title = parse_choice(ask(_system(), question), len(cands))
                target = cands[idx] if idx is not None else None
                title = new_title or title
            except (ValueError, RuntimeError):
                stats["ask_errors"] += 1  # не смогли спросить — безопаснее новый предмет, чем склейка разных решений
        if target is None:
            row = Subject(project_id=project_id, title=title, embedding=pack(vec))
            db.add(row)
            db.flush()
            target = _Subj(row, [], 0)
            subjects.append(target)
            stats["created"] += 1
        else:
            stats["attached"] += 1
        target.add(vec, act)
        lst = recent_of.setdefault(act.artifact_id, [])
        if target in lst:
            lst.remove(target)
        lst.append(target)
        del lst[:-RECENT_SUBJECTS - 2]
        target.row.embedding = pack(target.centroid)
        act.subject_id = target.row.id
    db.commit()
    stats["subjects"] = len(subjects)
    log_event(logger, "subjects.assigned", project=project_id, **{k: v for k, v in stats.items()})
    return stats


def build_events(db: Session, project_id: int) -> dict[str, int]:
    """События журнала из актов по их предметам (``decision_log.rebuild_events_from_acts``) и состояния предметов."""
    from app.app.services import decision_log as dl

    assignments = {a.id: a.subject_id for a in db.scalars(select(SpeechAct).where(SpeechAct.project_id == project_id, SpeechAct.subject_id.is_not(None))).all()}
    days = {a.id: _day_of(a) for a in db.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()}
    out = dl.rebuild_events_from_acts(db, project_id, assignments, day_of_artifact=days)
    db.commit()
    return out
