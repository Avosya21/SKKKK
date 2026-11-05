"""Режим памяти решений по проекту и автоматическая готовность (docs/plans/DECISION-MEMORY-STAGES-6-8.md, Э7).

``off`` — память не используется; ``shadow`` — строится, видна администратору, чат её не использует; ``live`` — видна менеджерам,
чат берёт из неё журнал решений. Режим проекта (``settings_json.decision_memory``) главнее общего ``DECISION_MEMORY_MODE``.
Человек ничего не размечает: готовность считается сама по проверкам ``readiness`` (построена, цитаты на месте, итоги судьи, согласие с
прежним контуром по вердиктам арбитра), и при ``DECISION_MEMORY_AUTO_LIVE`` проект сам переходит из shadow в live.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.decision_memory import MemoryShadowReview, Subject
from app.app.models.speech_act import SpeechAct

logger = logging.getLogger("primaryanalitics.memory_mode")

MODES = ("off", "shadow", "live")
SETTING_KEY = "decision_memory"
STALE_DAYS = 14  # «предложено/обсуждается» дольше — предмет считается зависшим
MIN_JUDGED_SHARE = 0.80  # доля предметов с решающими репликами, по которым судья вынес итог
MIN_ACCURACY = 0.85  # доля сравнённых пунктов, по которым память верна (совпала с прежним контуром или права в споре по арбитру)
MAX_BEHIND_OLD = 0.05  # насколько память может уступать прежнему контуру по этой доле
MIN_COMPARED = 10  # пунктов, сравнённых с прежним контуром, чтобы вывод имел смысл
DECISIVE_ACTS = ("proposed", "agreed", "rejected", "done", "cancelled", "documented")


def mode_of(settings: Settings, project: Any | None) -> str:
    value = settings.decision_memory_mode
    if project is not None:
        try:
            own = (project.settings() or {}).get(SETTING_KEY)
        except (AttributeError, ValueError, TypeError):
            own = None
        if own is not None:
            value = own
    value = str(value or "off").strip().lower()
    return value if value in MODES else "off"


def set_mode(db: Session, project: Any, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"режим: один из {', '.join(MODES)}")
    data = project.settings() or {}
    data[SETTING_KEY] = mode
    project.set_settings(data)
    db.commit()


def uses_in_chat(settings: Settings, project: Any | None) -> bool:
    """Чат берёт журнал решений только в режиме live."""
    return mode_of(settings, project) == "live"


def stale_count(db: Session, project_id: int, *, now: datetime | None = None) -> int:
    """Предметы в состоянии «предложено/обсуждается», у которых последнее движение старше ``STALE_DAYS`` дней."""
    limit = ((now or datetime.now(timezone.utc)) - timedelta(days=STALE_DAYS)).strftime("%Y-%m-%dT%H:%M")
    return int(db.scalar(select(func.count()).select_from(Subject).where(
        Subject.project_id == project_id, Subject.is_active.is_(True), Subject.state.in_(("proposed", "open")),
        Subject.state_since.is_not(None), Subject.state_since < limit)) or 0)


def _check(key: str, title: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"key": key, "title": title, "ok": bool(ok), "detail": detail}


def readiness(db: Session, project_id: int) -> dict[str, Any]:
    """Автоматические проверки готовности проекта к режиму live. Ни одна не требует ручной разметки."""
    subjects = db.scalars(select(Subject).where(Subject.project_id == project_id, Subject.is_active.is_(True))).all()
    acts = int(db.scalar(select(func.count()).select_from(SpeechAct).where(SpeechAct.project_id == project_id)) or 0)
    no_quote = int(db.scalar(select(func.count()).select_from(SpeechAct).where(SpeechAct.project_id == project_id, func.length(func.trim(SpeechAct.quote)) == 0)) or 0)
    from app.app.services.subject_judge import needs_judge

    by_subject: dict[int, list[SpeechAct]] = {}
    for act in db.scalars(select(SpeechAct).where(SpeechAct.project_id == project_id, SpeechAct.act.in_(DECISIVE_ACTS),
                                                  SpeechAct.subject_id.is_not(None))).all():
        by_subject.setdefault(act.subject_id, []).append(act)
    decisive = {sid for sid, acts_of in by_subject.items() if needs_judge(acts_of)}  # предметам одного документа итог судьи не нужен
    judged = sum(1 for s in subjects if s.id in decisive and s.state_source in ("judge", "human"))
    share = judged / len(decisive) if decisive else 0.0
    reviews = db.scalars(select(MemoryShadowReview).where(MemoryShadowReview.project_id == project_id)).all()
    new_r = sum(1 for r in reviews if r.verdict == "new")
    old_r = sum(1 for r in reviews if r.verdict == "old")
    agree = sum(1 for r in reviews if r.verdict == "agree")
    compared = len(reviews)
    decided = agree + new_r + old_r  # без «неясно» и «обе неверны»: по ним арбитр ответа не дал
    mem_acc = (agree + new_r) / decided if decided else 0.0
    old_acc = (agree + old_r) / decided if decided else 0.0
    checks = [
        _check("built", "Память построена", bool(subjects) and acts > 0, f"предметов {len(subjects)}, реплик-актов {acts}"),
        _check("quotes", "У каждого акта есть цитата источника", acts > 0 and no_quote == 0, f"без цитаты {no_quote}"),
        _check("judged", f"Итог вынесен у {int(MIN_JUDGED_SHARE * 100)} % предметов с решениями", share >= MIN_JUDGED_SHARE and bool(decisive),
               f"{judged} из {len(decisive)}"),
        _check("compared", "Сравнение с прежним контуром выполнено", compared >= MIN_COMPARED, f"пунктов сравнено {compared}"),
        _check("agreement", f"Память верна не менее чем в {int(MIN_ACCURACY * 100)} % сравнённых пунктов и уступает прежнему контуру не более чем на {int(MAX_BEHIND_OLD * 100)} п.п.",
               compared >= MIN_COMPARED and mem_acc >= MIN_ACCURACY and mem_acc >= old_acc - MAX_BEHIND_OLD,
               f"память {round(mem_acc * 100)} %, прежний контур {round(old_acc * 100)} % (совпало {agree}, права память {new_r}, прежний контур {old_r})"),
    ]
    return {"ready": all(c["ok"] for c in checks), "checks": checks}


def maybe_promote(db: Session, settings: Settings, project: Any) -> str | None:
    """shadow -> live, когда проект готов и включён ``DECISION_MEMORY_AUTO_LIVE``. Возвращает новый режим или None."""
    if not settings.decision_memory_auto_live or mode_of(settings, project) != "shadow":
        return None
    if not readiness(db, project.id)["ready"]:
        return None
    set_mode(db, project, "live")
    log_event(logger, "decision_memory.promoted", project=project.id, mode="live")
    return "live"
