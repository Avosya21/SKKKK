"""Авторы речевых актов созвона — по текущей стенограмме (docs/plans/DECISION-MEMORY-PLAN.md).

Акт получает автора при разборе (``speech_acts.extract_artifact``): имя говорящего из ``_final.txt`` или метку
неназванного голоса (``speaker_2``). Голоса часто опознают позже разбора — тогда стенограмма уже с именами, а акты и
события журнала остаются с меткой и без человека. ``rebind_call_authors`` переносит имена из стенограммы в акты
(автор, человек, сторона) и в события журнала, выведенные из этих актов, и пересчитывает затронутые предметы свёрткой
журнала, кроме предметов с итогом судьи или человека (``people.sync_decision_memory``). Модель не вызывается. Реплика акта — по номеру ``T<n>`` и дословной цитате (номера сдвигаются после «Слить» —
тогда ближайшая по ``start_sec`` реплика с цитатой); не нашлась — метка голоса → имя подтверждённого спикера созвона. Вызывается из ``entities.rebind_call_entities`` — там же, где
обновляются имена в сущностях созвона (подтверждение и переименование голосов, переиндексация).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.person import SIDE_CUSTOMER, SIDE_EXECUTOR

logger = logging.getLogger("skkkk.act_authors")


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def _turn_of(act: Any, turns: list, by_index: dict[int, Any]) -> Any | None:
    """Реплика акта: та, чей номер в ``unit_ref``, если цитата в ней; иначе ближайшая по времени реплика с цитатой
    (после «Слить» номера сдвигаются). Только по времени нельзя: у соседних реплик бывает одна и та же секунда начала."""
    quote = _norm(act.quote)
    if not quote:
        return None
    ref = str(act.unit_ref or "")
    turn = by_index.get(int(ref[1:])) if ref[:1] == "T" and ref[1:].isdigit() else None
    if turn is not None and quote in _norm(turn.text):
        return turn
    hits = [t for t in turns if t.is_speech and quote in _norm(t.text)]
    if not hits:
        return None
    if act.start_sec is None:
        return hits[0] if len(hits) == 1 else None
    return min(hits, key=lambda t: abs((t.start_sec if t.start_sec is not None else 1e9) - act.start_sec))


def _new_author(act: Any, turns: list, by_index: dict[int, Any], label_names: dict[str, str]) -> str | None:
    """Автор акта по стенограмме — как его дал бы разбор сейчас; реплики не нашлось — имя подтверждённого спикера
    для метки голоса."""
    from app.app.services.transcript_turns import PLACEHOLDER_SPEAKER

    turn = _turn_of(act, turns, by_index)
    if turn is not None:
        return turn.known_speaker or turn.speaker or act.author
    author = (act.author or "").strip()
    if PLACEHOLDER_SPEAKER.match(author):
        return label_names.get(author.casefold()) or act.author
    return act.author


def rebind_call_authors(db: Session, settings: Settings, artifact: Artifact) -> dict[str, int]:
    """Имена из стенограммы созвона — в акты памяти решений и события журнала этих актов. Возвращает счётчики:
    ``acts`` — у скольких актов сменился автор, ``people`` — сколько актов получили человека, ``events``, ``subjects``."""
    stats = {"acts": 0, "people": 0, "events": 0, "subjects": 0}
    if artifact.file_type != "audio":
        return stats
    from app.app.models.decision_memory import DecisionEvent
    from app.app.models.speech_act import SpeechAct
    from app.app.services import decision_log, people
    from app.app.services.entities import speaker_label_names
    from app.app.services.transcript_turns import PLACEHOLDER_SPEAKER, load_turns

    acts = db.scalars(select(SpeechAct).where(SpeechAct.artifact_id == artifact.id)).all()
    if not acts:
        return stats
    turns = load_turns(settings, artifact)
    by_index = {t.index: t for t in turns}
    label_names = speaker_label_names(db, artifact)
    cards: dict[str, tuple[int | None, str | None]] = {}

    def card(author: str) -> tuple[int | None, str | None]:
        if author not in cards:
            hit = people.resolve(db, artifact.project_id, author)
            cards[author] = (hit.person.id, hit.person.side) if hit is not None else (None, None)
        return cards[author]

    changed: dict[int, str | None] = {}  # id акта -> прежний автор
    for act in acts:
        author = _new_author(act, turns, by_index, label_names)
        if author and author != act.author:
            changed[act.id] = act.author
            person_id, side = card(author)
            if person_id is None and not PLACEHOLDER_SPEAKER.match((act.author or "").strip()):
                person_id, side = act.person_id, act.side  # имя голоса уточнили («… (отдел)»), а написания в реестре нет — человек тот же
            elif person_id == act.person_id and side not in (SIDE_EXECUTOR, SIDE_CUSTOMER):
                side = act.side
            act.author, act.person_id, act.side = author, person_id, side
            stats["acts"] += 1
            stats["people"] += int(act.person_id is not None)
        elif act.person_id is None and act.author:
            person_id, side = card(act.author)  # карточку завели позже разбора, а написание то же
            if person_id is not None:
                changed[act.id] = act.author
                act.person_id, act.side = person_id, side
                stats["people"] += 1
    if not changed:
        return stats
    db.flush()
    by_id = {a.id: a for a in acts}
    touched: set[int] = set()
    for event in db.scalars(select(DecisionEvent).where(DecisionEvent.speech_act_id.in_(changed))).all():
        act, old = by_id[event.speech_act_id], changed[event.speech_act_id]
        if event.actor_label not in (None, "", old, act.author):
            continue  # автора события правили отдельно — не трогаем
        new = (act.author, act.person_id, decision_log.normalize_side(act.side))
        if (event.actor_label, event.actor_person_id, event.actor_side) != new:
            event.actor_label, event.actor_person_id, event.actor_side = new
            touched.add(event.subject_id)
            stats["events"] += 1
    db.flush()
    # Пересчёт свёрткой — как при правке людей: итог судьи или человека не перетирается (судья перепроверит сам)
    people.sync_decision_memory(db, artifact.project_id, person_ids=set(), subject_ids=touched)
    stats["subjects"] = len(touched)
    log_event(logger, "act_authors.rebound", artifact=artifact.artifact_id, **stats)
    return stats
