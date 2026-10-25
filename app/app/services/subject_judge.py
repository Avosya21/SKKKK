"""Итог предмета: модель-судья по короткой ленте актов (docs/plans/DECISION-MEMORY-PLAN.md, Э5).

Свёртка журнала (``decision_log.fold``) механически считает состояние по видам событий и сторонам, но не понимает смысла реплик:
«давайте сделаем так, как вы показали» в ответ на отказ — это согласие с отказом, а свёртка увидит «согласилась сторона заказчика».
Судья читает ленту одного предмета (до ``MAX_LINES`` актов, цитаты и стороны) и отвечает одним узким вопросом: чем закончилось и
на какие реплики опирается. Код проверяет ответ: состояние из списка, ``basis`` — непустой набор номеров из показанной ленты.
Решение человека судья не трогает. Принятый вердикт пишется в предмет (``state_source=judge``, итог словами, номера актов);
отвергнутый — оставляет состояние свёртки. Следующая пересборка журнала возвращает состояние к свёртке — судью запускают заново.
Предмет, о котором есть только один документ и в нём нет противоречий (``needs_judge``), судье не нужен: спорить не о чем, итог —
свёртка («в документе», «выполнено» …). Так документ на сотни пунктов не превращается в сотни вызовов модели.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.decision_memory import POSITION_STATES, STATES, DecisionEvent, Subject, SubjectPosition
from app.app.models.speech_act import ACT_LABELS, SpeechAct

logger = logging.getLogger("primaryanalitics.subject_judge")

PROMPT_VERSION = "subject_judge-v3"  # v3: акты документов («зафиксировано в документе») и состояние documented
MAX_POSITIONS = 8
LABEL_MAX = 160
MAX_LINES = 15  # актов в ленте; длиннее — первые два и последние
SIDE_RU = {"customer": "заказчик", "executor": "исполнитель"}
EVENT_ACTS = ("proposed", "agreed", "rejected", "done", "cancelled", "documented")  # субъект без таких актов судье не нужен
DOC_LINES_PER_DOC = 3  # пунктов одного документа в ленте судьи
DOC_PRIORITY = {"cancelled": 0, "rejected": 0, "done": 1, "agreed": 1, "deferred": 1, "documented": 2}
SETTLED = ("agreed", "implemented", "rejected", "superseded", "withdrawn")  # «в документе» эти состояния не понижает


def needs_judge(acts: list[Any]) -> bool:
    """Нужен ли предмету судья: есть решающие акты, и среди них реплика созвона/переписки, либо документов несколько, либо один
    документ говорит о предмете разное (например, «зафиксировано» и «отменено»)."""
    decisive = [a for a in acts if a.act in EVENT_ACTS]
    if not decisive:
        return False
    if any(not str(a.unit_ref).startswith("P") for a in decisive):
        return True
    return len({a.artifact_id for a in decisive}) > 1 or len({a.act for a in decisive}) > 1


def _system() -> str:
    from app.app.services.prompts import load_prompt_block
    from app.app.services.speech_acts import ROOT

    return load_prompt_block(ROOT / "docs" / "prompts" / "subject_judge.md")


def _ask_default(settings: Settings) -> Callable[[str, str], str]:
    from app.app.services.llm import chat_completion_no_think

    def ask(system: str, user: str) -> str:
        return chat_completion_no_think(settings, system=system, user=user, max_tokens=900)

    return ask


def _when(act: SpeechAct, days: dict[int, str]) -> str:
    if act.said_at:
        return str(act.said_at)[:16].replace("T", " ")
    return f"{days.get(act.artifact_id, '????-??-??')} (время неизвестно)"


def _is_doc(act: Any) -> bool:
    return str(getattr(act, "unit_ref", "") or "").startswith("P")


def timeline_acts(acts: list[Any]) -> list[Any]:
    """Лента для судьи по времени. От каждого документа — не больше ``DOC_LINES_PER_DOC`` пунктов (сначала отмена, отказ, «сделано»,
    потом «зафиксировано»): иначе десяток пунктов одного документа вытесняет из ленты согласие на созвоне (проект 18, предмет
    «вариант структуры номенклатуры», 2026-10-09). Реплик больше места, чем осталось, — две первые и последние."""
    by_doc: dict[int, list[Any]] = {}
    for a in acts:
        if _is_doc(a):
            by_doc.setdefault(a.artifact_id, []).append(a)
    keep: set[int] = set()
    for items in by_doc.values():
        ranked = sorted(range(len(items)), key=lambda i: (DOC_PRIORITY.get(items[i].act, 3), i))
        keep.update(id(items[i]) for i in ranked[:DOC_LINES_PER_DOC])
    acts = [a for a in acts if not _is_doc(a) or id(a) in keep]
    if len(acts) <= MAX_LINES:
        return acts
    dialog = [a for a in acts if not _is_doc(a)]
    room = max(4, MAX_LINES - len(keep))
    if len(dialog) > room:
        dialog = dialog[:2] + dialog[-(room - 2):]
    chosen = {id(a) for a in dialog} | keep
    return [a for a in acts if id(a) in chosen]


def build_user(subject: Subject, acts: list[SpeechAct], days: dict[int, str]) -> str:
    lines = [f"Предмет: {subject.title}", "", "Лента:"]
    for a in acts:
        side = f" [{SIDE_RU[a.side]}]" if a.side in SIDE_RU else ""
        lines.append(f"A{a.id} {_when(a, days)}{side} {a.author or '?'} — {ACT_LABELS.get(a.act, a.act)}: «{a.quote}»")
    return "\n".join(lines)


def parse_verdict(raw: str, shown_ids: set[int]) -> dict[str, Any]:
    """Ответ судьи -> {state, basis: [номера актов], summary}; невалидный — ValueError."""
    from app.app.services.llm import strip_json_fence

    data = json.loads(strip_json_fence(raw))
    if not isinstance(data, dict):
        raise ValueError("ответ не объект")
    state = str(data.get("state") or "").strip().lower()
    if state not in STATES:
        raise ValueError(f"состояние не из списка: {state!r}")
    basis: list[int] = []
    for item in data.get("basis") or []:
        m = re.search(r"\d+", str(item))
        if m and int(m.group(0)) in shown_ids and int(m.group(0)) not in basis:
            basis.append(int(m.group(0)))
    if not basis:
        raise ValueError("не названы реплики-основания из ленты")
    return {"state": state, "basis": basis, "summary": str(data.get("summary") or "").strip()[:500], "positions": _parse_positions(data.get("positions"), shown_ids)}


def _parse_positions(raw: Any, shown_ids: set[int]) -> list[dict[str, Any]]:
    """Позиции (варианты и значения внутри предмета): неверное состояние или пустое название отбрасывают позицию, лишнее — обрезается."""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:LABEL_MAX]
        state = str(item.get("state") or "").strip().lower()
        if not label or state not in POSITION_STATES or any(p["label"] == label for p in out):
            continue
        basis = []
        for b in item.get("basis") or []:
            m = re.search(r"\d+", str(b))
            if m and int(m.group(0)) in shown_ids and int(m.group(0)) not in basis:
                basis.append(int(m.group(0)))
        replaced = str(item.get("replaced_by") or "").strip()[:LABEL_MAX] or None
        out.append({"label": label, "state": state, "basis": basis, "replaced_by": replaced if state == "superseded" else None})
        if len(out) >= MAX_POSITIONS:
            break
    return out


def has_human_decision(db: Session, subject_id: int) -> bool:
    return db.scalar(select(DecisionEvent.id).where(DecisionEvent.subject_id == subject_id, DecisionEvent.decided_by == "human",
                                                   DecisionEvent.is_active.is_(True)).limit(1)) is not None


def judge_subject(db: Session, settings: Settings, subject: Subject, *, days: dict[int, str] | None = None,
                  ask: Callable[[str, str], str] | None = None) -> dict[str, Any]:
    """Итог одного предмета. Возвращает {status: applied|kept|skipped|error, ...}."""
    if has_human_decision(db, subject.id):
        return {"status": "skipped", "reason": "решение человека"}
    acts = list(db.scalars(select(SpeechAct).where(SpeechAct.subject_id == subject.id).order_by(SpeechAct.said_at, SpeechAct.artifact_id, SpeechAct.unit_key)).all())
    if not any(a.act in EVENT_ACTS for a in acts):
        return {"status": "skipped", "reason": "нет предложений и решений"}
    if not needs_judge(acts):
        return {"status": "skipped", "reason": "один документ без противоречий: итог — свёртка"}
    shown = timeline_acts(acts)
    days = days or {}
    ask = ask or _ask_default(settings)
    user = build_user(subject, shown, days)
    system = _system()
    verdict: dict[str, Any] | None = None
    last_error = ""
    for attempt in range(2):
        db.commit()  # соединение БД не держим на время ответа модели
        try:
            verdict = parse_verdict(ask(system, user), {a.id for a in shown})
            break
        except (ValueError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = str(exc)[:200]
            if attempt == 0:
                user += f"\n\nПредыдущий ответ невалиден: {last_error}. Верни только JSON вида {{\"state\": ..., \"basis\": [...], \"summary\": ...}}."
    if verdict is None:
        return {"status": "error", "error": last_error}
    if verdict["state"] == "documented" and subject.state in SETTLED:
        # правило владельца (DECISIONS 2026-10-09): документ согласованное и решённое не понижает — остаётся итог свёртки
        log_event(logger, "subject_judge.documented_refused", subject=subject.id, fold=subject.state)
        return {"status": "kept", "reason": "документ не понижает решённое", "state": subject.state}
    if not verdict["positions"] and (any(a.act in ("rejected", "cancelled") for a in shown) or sum(a.act == "proposed" for a in shown) >= 2):
        # в ленте есть отказ от варианта или несколько предложений, а позиции не названы: один раз просим перечислить позиции (отклонённый и принятый вариант)
        hint = user + ("\n\nВ ленте несколько вариантов или есть отказ от варианта. Перечисли позиции: какой вариант отклонён или заменён, какой действует. "
                       "Верни тот же JSON, но с заполненным списком positions.")
        db.commit()
        try:
            again = parse_verdict(ask(system, hint), {a.id for a in shown})
            if again["positions"]:
                verdict = again
        except (ValueError, json.JSONDecodeError, RuntimeError):
            pass  # первый вердикт остаётся
    by_id = {a.id: a for a in shown}
    last_basis = max((by_id[i] for i in verdict["basis"]), key=lambda a: (a.said_at or days.get(a.artifact_id, ""), a.id))
    before = subject.state
    subject.state, subject.state_source = verdict["state"], "judge"
    subject.state_since = last_basis.said_at or days.get(last_basis.artifact_id)
    subject.summary, subject.basis_json = verdict["summary"] or None, json.dumps(verdict["basis"])
    subject.judged_sig = act_signature(db, subject.id)
    for old_pos in db.scalars(select(SubjectPosition).where(SubjectPosition.subject_id == subject.id)).all():
        db.delete(old_pos)
    for pos in verdict["positions"]:
        db.add(SubjectPosition(subject_id=subject.id, label=pos["label"], state=pos["state"], basis_json=json.dumps(pos["basis"]), replaced_by=pos["replaced_by"]))
    return {"status": "applied", "state": verdict["state"], "was": before, "changed": before != verdict["state"], "basis": verdict["basis"]}


def act_signature(db: Session, subject_id: int) -> str:
    """Отпечаток ленты предмета: «число актов:наибольший номер». Меняется, когда в предмет добавили или убрали акты."""
    from sqlalchemy import func

    count, top = db.execute(select(func.count(), func.max(SpeechAct.id)).where(SpeechAct.subject_id == subject_id)).one()
    return f"{count}:{top or 0}"


def snapshot_judged(db: Session, project_id: int) -> dict[int, dict[str, Any]]:
    """Итоги судьи, которые надо сохранить на время пересборки событий (она сбрасывает состояние предметов к свёртке)."""
    out: dict[int, dict[str, Any]] = {}
    for s in db.scalars(select(Subject).where(Subject.project_id == project_id, Subject.state_source == "judge", Subject.judged_sig.is_not(None))).all():
        out[s.id] = {"state": s.state, "since": s.state_since, "source": s.state_source, "summary": s.summary, "basis": s.basis_json, "sig": s.judged_sig}
    return out


def restore_unchanged(db: Session, snapshot: dict[int, dict[str, Any]]) -> int:
    """Вернуть итоги судьи предметам, у которых лента не изменилась (отпечаток тот же); остальные будут оценены заново."""
    restored = 0
    for sid, snap in snapshot.items():
        s = db.get(Subject, sid)
        if s is None or not s.is_active or act_signature(db, sid) != snap["sig"]:
            continue
        s.state, s.state_since, s.state_source, s.summary, s.basis_json, s.judged_sig = (snap["state"], snap["since"], snap["source"], snap["summary"],
                                                                                        snap["basis"], snap["sig"])
        restored += 1
    db.commit()
    return restored


def judge_project(db: Session, settings: Settings, project_id: int, *, ask: Callable[[str, str], str] | None = None,
                  progress: Callable[[str], None] | None = None, only_pending: bool = False) -> dict[str, int]:
    """Итоги всех предметов проекта. Сначала состояние предметов пересчитывается по журналу (свёртка), затем судья.
    ``only_pending`` — только предметы без итога судьи (инкремент: остальным итоги вернул ``restore_unchanged``)."""
    from app.app.models.artifact import Artifact
    from app.app.services import decision_log as dl
    from app.app.services.subjects import _day_of

    days = {a.id: _day_of(a) for a in db.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()}
    subjects = list(db.scalars(select(Subject).where(Subject.project_id == project_id, Subject.is_active.is_(True)).order_by(Subject.id)).all())
    if only_pending:
        subjects = [s for s in subjects if s.state_source != "judge"]
    for s in subjects:
        dl.refresh_subject(db, s)
    db.commit()
    counts = {"subjects": len(subjects), "applied": 0, "changed": 0, "skipped": 0, "errors": 0}
    for s in subjects:
        result = judge_subject(db, settings, s, days=days, ask=ask)
        db.commit()
        if result["status"] == "applied":
            counts["applied"] += 1
            counts["changed"] += bool(result["changed"])
        elif result["status"] == "error":
            counts["errors"] += 1
            log_event(logger, "subject_judge.error", subject=s.id, error=result.get("error"))
        else:
            counts["skipped"] += 1
        if progress and result["status"] == "applied" and result["changed"]:
            progress(f"предмет #{s.id} «{s.title[:50]}»: {result['was']} -> {result['state']}")
    return counts
