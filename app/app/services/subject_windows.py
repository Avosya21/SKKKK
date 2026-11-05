"""Предметы одним проходом модели по окну актов (docs/plans/DECISION-MEMORY-PLAN.md, §13, вариант 2).

Жадная раскладка по близости векторов (``subjects.assign``) то дробит решение на предметы из одной реплики, то склеивает разные. Здесь
модель видит окно из ``WINDOW_ACTS`` актов и список уже известных предметов (ближайшие по вектору и последние тронутые) и для каждого
акта выбирает предмет или объявляет новый: общая метка N<k> на несколько актов окна, название — отдельно. Код проверяет метки: акт — из
окна, предмет — из показанных. Акт, который модель не разметила, получает отдельный предмет (безопаснее, чем склейка разных решений).
Центр предмета (среднее векторов актов) пишется в ``subjects.embedding`` — по нему чат подбирает предметы к вопросу.
Промпт — ``docs/prompts/subject_window.md``.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.logging_setup import log_event
from app.app.models.artifact import Artifact
from app.app.models.decision_memory import DecisionEvent, MemoryShadowReview, Subject, SubjectPosition
from app.app.models.speech_act import ACT_LABELS, SpeechAct
from app.app.services.subjects import TITLE_MAX, _day_of, _sort_key, _Subj, act_text, cosine, pack, unpack

logger = logging.getLogger("primaryanalitics.subject_windows")

PROMPT_VERSION = "subject_window-v2"  # v2: пункты документов — раздел не предмет, каждое требование отдельно
WINDOW_ACTS = 25  # актов в одном вопросе модели
KNOWN_SIMILAR = 12  # известных предметов, ближайших по вектору к окну
KNOWN_RECENT = 5  # и последних тронутых
MAX_ANSWER_TOKENS = 2000  # метка на каждый акт окна и названия новых предметов


def _ask_default(settings: Settings) -> Callable[[str, str], str]:
    from app.app.services.llm import chat_completion_no_think

    def ask(system: str, user: str) -> str:
        return chat_completion_no_think(settings, system=system, user=user, max_tokens=MAX_ANSWER_TOKENS)

    return ask


def _system() -> str:
    from app.app.services.prompts import load_prompt_block
    from app.app.services.speech_acts import ROOT

    return load_prompt_block(ROOT / "docs" / "prompts" / "subject_window.md")


def build_question(acts: list[SpeechAct], known: list[_Subj]) -> str:
    lines = ["Известные предметы:"]
    if not known:
        lines.append("(пока нет)")
    for k in known:
        lines.append(f"S{k.row.id} {k.row.title}")
        lines.extend(f"   · {r}" for r in k.recent)
    lines += ["", "Акты окна:"]
    for a in acts:
        about = f" (о чём: {a.about})" if a.about else ""
        lines.append(f"A{a.id} [{a.author or '?'}] {ACT_LABELS.get(a.act, a.act)}: «{a.quote[:260]}»{about}")
    return "\n".join(lines)


def parse_window(raw: str, act_ids: set[int], subject_ids: set[int]) -> tuple[dict[int, str], dict[str, str]]:
    """Ответ модели -> ({id акта: «S<id>» или «N<k>»}, {«N<k>»: название}). Неизвестные акты и метки отбрасываются; ни одного
    размеченного акта или не JSON — ошибка."""
    from app.app.services.llm import strip_json_fence

    data = json.loads(strip_json_fence(raw))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("нет списка items")
    out: dict[int, str] = {}
    for it in data["items"]:
        if not isinstance(it, dict):
            continue
        m = re.fullmatch(r"A?(\d+)", str(it.get("act") or "").strip())
        label = str(it.get("subject") or "").strip().upper()
        if not m or int(m.group(1)) not in act_ids:
            continue
        ms = re.fullmatch(r"S(\d+)", label)
        if (ms and int(ms.group(1)) in subject_ids) or re.fullmatch(r"N\d+", label):
            out[int(m.group(1))] = label
    raw_titles = data.get("titles")
    titles = {str(k).strip().upper(): v.strip()[:TITLE_MAX] for k, v in raw_titles.items() if isinstance(v, str)} if isinstance(raw_titles, dict) else {}
    if not out:
        raise ValueError("ни один акт окна не размечен")
    return out, titles


def assign(db: Session, settings: Settings, project_id: int, *, rebuild: bool = False,
           embed: Callable[[list[str]], list[list[float]]] | None = None, ask: Callable[[str, str], str] | None = None) -> dict[str, Any]:
    """Назначить предметы актам проекта без предмета (``rebuild`` — все заново, кроме предметов с решением человека)."""
    from app.app.services.ollama_client import embed_texts

    embed = embed or (lambda texts: embed_texts(settings, texts))
    ask = ask or _ask_default(settings)
    if rebuild:
        keep = set(db.scalars(select(DecisionEvent.subject_id).where(DecisionEvent.project_id == project_id, DecisionEvent.decided_by == "human")).all())
        drop = [x for x in db.scalars(select(Subject.id).where(Subject.project_id == project_id)).all() if x not in keep]
        if drop:
            db.execute(update(SpeechAct).where(SpeechAct.subject_id.in_(drop)).values(subject_id=None))
            db.execute(delete(DecisionEvent).where(DecisionEvent.subject_id.in_(drop)))
            db.execute(delete(SubjectPosition).where(SubjectPosition.subject_id.in_(drop)))
            db.execute(delete(Subject).where(Subject.id.in_(drop)))
        db.flush()
    known: dict[int, _Subj] = {}
    for row in db.scalars(select(Subject).where(Subject.project_id == project_id, Subject.is_active.is_(True))).all():
        count = int(db.scalar(select(func.count()).select_from(SpeechAct).where(SpeechAct.subject_id == row.id)) or 0)
        known[row.id] = _Subj(row, unpack(row.embedding), count)
    pending = list(db.scalars(select(SpeechAct).where(SpeechAct.project_id == project_id, SpeechAct.subject_id.is_(None))).all())
    stats: dict[str, Any] = {"acts": len(pending), "windows": 0, "attached": 0, "created": 0, "unmarked": 0, "errors": 0, "subjects": len(known)}
    if not pending:
        return stats
    days = {a.id: _day_of(a) for a in db.scalars(select(Artifact).where(Artifact.project_id == project_id)).all()}
    pending.sort(key=lambda a: _sort_key(a, days))
    system = _system()
    db.commit()  # соединение БД не держим на время ответов модели
    vectors = embed([act_text(a) for a in pending])
    if len(vectors) != len(pending):
        raise RuntimeError(f"векторов {len(vectors)} на {len(pending)} актов")
    vec_of = {a.id: v for a, v in zip(pending, vectors)}
    touched: list[int] = []  # недавно тронутые предметы, последний — в конце
    for i in range(0, len(pending), WINDOW_ACTS):
        window = pending[i:i + WINDOW_ACTS]
        ranked = sorted(known.values(), key=lambda k: -max(cosine(vec_of[a.id], k.centroid) for a in window))
        shown = ranked[:KNOWN_SIMILAR]
        for sid in reversed(touched):
            if len(shown) >= KNOWN_SIMILAR + KNOWN_RECENT:
                break
            if known[sid] not in shown:
                shown.append(known[sid])
        question = build_question(window, shown)
        shown_ids = {k.row.id for k in shown}
        act_ids = {a.id for a in window}
        marks: dict[int, str] = {}
        titles: dict[str, str] = {}
        db.commit()
        for attempt in range(2):
            try:
                marks, titles = parse_window(ask(system, question), act_ids, shown_ids)
                break
            except (ValueError, RuntimeError) as exc:
                if attempt == 0:
                    question += f'\n\nПредыдущий ответ невалиден: {exc}. Верни только JSON вида {{"items": [...], "titles": {{...}}}}.'
                else:
                    stats["errors"] += 1
        missing = [a for a in window if a.id not in marks]
        if missing and len(missing) < len(window):  # модель разметила не все акты окна: один раз спрашиваем про оставшиеся
            top = max([int(m[1:]) for m in marks.values() if m.startswith("N")] + [0])
            named = "\n".join(f"{label} {title}" for label, title in sorted(titles.items()) if label in set(marks.values()))
            only = f"Разметь ТОЛЬКО акты ниже; новые метки начинай с N{top + 1}."
            extra = f"\n\nВ этом окне уже названы новые предметы:\n{named}\n{only}" if named else f"\n\n{only}"
            follow = build_question(missing, shown) + extra
            db.commit()
            try:
                more, more_titles = parse_window(ask(system, follow), {a.id for a in missing}, shown_ids | {int(m[1:]) for m in marks.values() if m.startswith("S")})
                marks.update(more)
                titles.update({k: v for k, v in more_titles.items() if k not in titles})
                stats["followups"] = stats.get("followups", 0) + 1
            except (ValueError, RuntimeError):
                stats["errors"] += 1
        stats["windows"] += 1
        new_rows: dict[str, _Subj] = {}
        for act in window:
            label, vec = marks.get(act.id), vec_of[act.id]
            target: _Subj | None = None
            if label and label.startswith("S"):
                target = known[int(label[1:])]
                stats["attached"] += 1
            elif label:
                target = new_rows.get(label)
                if target is None:
                    row = Subject(project_id=project_id, title=titles.get(label) or (act.about or act.quote)[:TITLE_MAX], embedding=pack(vec))
                    db.add(row)
                    db.flush()
                    target = new_rows[label] = known[row.id] = _Subj(row, [], 0)
                    stats["created"] += 1
            if target is None:
                stats["unmarked"] += 1
                row = Subject(project_id=project_id, title=(act.about or act.quote)[:TITLE_MAX], embedding=pack(vec))
                db.add(row)
                db.flush()
                target = known[row.id] = _Subj(row, [], 0)
                stats["created"] += 1
            target.add(vec, act)
            target.row.embedding = pack(target.centroid)
            act.subject_id = target.row.id
            if target.row.id in touched:
                touched.remove(target.row.id)
            touched.append(target.row.id)
        db.commit()
    stats["subjects"] = len(known)
    log_event(logger, "subjects.assigned_windows", project=project_id, **stats)
    return stats


def release_document_acts(db: Session, project_id: int) -> dict[str, int]:
    """Снять с предметов акты документов (``P<n>``), чтобы следующий ``assign`` разложил их заново (смена промпта раскладки).
    Предмет только из документов удаляется со своими событиями, позициями и строками сравнения; у предмета с репликами снимаются
    только акты документов. Предметы с решением человека не трогаются. Реплики созвонов и переписки остаются на своих предметах."""
    human = set(db.scalars(select(DecisionEvent.subject_id).where(DecisionEvent.project_id == project_id, DecisionEvent.decided_by == "human")).all())
    by_subject: dict[int, list[SpeechAct]] = {}
    for act in db.scalars(select(SpeechAct).where(SpeechAct.project_id == project_id, SpeechAct.subject_id.is_not(None))).all():
        by_subject.setdefault(act.subject_id, []).append(act)
    stats = {"released": 0, "dropped_subjects": 0, "kept_human": 0}
    drop: list[int] = []
    for sid, acts in by_subject.items():
        doc = [a for a in acts if str(a.unit_ref).startswith("P")]
        if not doc:
            continue
        if sid in human:
            stats["kept_human"] += 1
            continue
        for a in doc:
            a.subject_id = None
        stats["released"] += len(doc)
        if len(doc) == len(acts):
            drop.append(sid)
    db.flush()  # снятые акты — в базу до удаления их прежних предметов
    if drop:
        db.execute(delete(DecisionEvent).where(DecisionEvent.subject_id.in_(drop)))
        db.execute(delete(SubjectPosition).where(SubjectPosition.subject_id.in_(drop)))
        db.execute(delete(MemoryShadowReview).where(MemoryShadowReview.subject_id.in_(drop)))
        db.execute(update(Subject).where(Subject.superseded_by.in_(drop)).values(superseded_by=None))
        db.execute(delete(Subject).where(Subject.id.in_(drop)))
    stats["dropped_subjects"] = len(drop)
    db.commit()
    log_event(logger, "subjects.document_acts_released", project=project_id, **stats)
    return stats


MERGE_MIN_SIMILARITY = 0.70  # центры предметов ближе — пара идёт на вопрос «один ли это вопрос»
MERGE_MAX_PAIRS = 80
MERGE_QUOTES = 4
MERGE_PROMPT_VERSION = "subject_merge-v1"


def _merge_system() -> str:
    from app.app.services.prompts import load_prompt_block
    from app.app.services.speech_acts import ROOT

    return load_prompt_block(ROOT / "docs" / "prompts" / "subject_merge.md")


def _merge_question(db: Session, a: Subject, b: Subject) -> str:
    parts = []
    for s in (a, b):
        acts = db.scalars(select(SpeechAct).where(SpeechAct.subject_id == s.id).order_by(SpeechAct.said_at, SpeechAct.unit_key)).all()
        parts.append(f"Предмет «{s.title}»\n" + "\n".join(f"   · {(x.said_at or '')[:10]} {x.author or '?'}: {x.quote[:200]}" for x in acts[:MERGE_QUOTES]))
    return "\n\n".join(parts)


def parse_same(raw: str) -> bool:
    from app.app.services.llm import strip_json_fence

    data = json.loads(strip_json_fence(raw))
    if not isinstance(data, dict) or not isinstance(data.get("same"), bool):
        raise ValueError("нет булевого same")
    return data["same"]


def merge_similar(db: Session, settings: Settings, project_id: int, *, ask: Callable[[str, str], str] | None = None) -> dict[str, int]:
    """Склеить дубли: пары предметов с близкими центрами идут моделью на вопрос «один ли это вопрос». Акты меньшего предмета переходят в
    больший, меньший удаляется, центр пересчитывается. Предметы с решением человека не трогаются. Запускать до построения событий."""
    ask = ask or _ask_default(settings)
    subs = [s for s in db.scalars(select(Subject).where(Subject.project_id == project_id, Subject.is_active.is_(True))).all() if s.embedding]
    human = set(db.scalars(select(DecisionEvent.subject_id).where(DecisionEvent.project_id == project_id, DecisionEvent.decided_by == "human")).all())
    counts = {s.id: int(db.scalar(select(func.count()).select_from(SpeechAct).where(SpeechAct.subject_id == s.id)) or 0) for s in subs}
    vecs = {s.id: unpack(s.embedding) for s in subs}
    pairs = sorted(((cosine(vecs[a.id], vecs[b.id]), a, b) for i, a in enumerate(subs) for b in subs[i + 1:]), key=lambda t: -t[0])
    pairs = [p for p in pairs if p[0] >= MERGE_MIN_SIMILARITY][:MERGE_MAX_PAIRS]
    stats = {"pairs": len(pairs), "merged": 0, "errors": 0}
    gone: set[int] = set()
    system = _merge_system()
    for _sim, a, b in pairs:
        if a.id in gone or b.id in gone or a.id in human or b.id in human:
            continue
        question = _merge_question(db, a, b)
        db.commit()
        try:
            same = parse_same(ask(system, question))
        except (ValueError, RuntimeError):
            stats["errors"] += 1
            continue
        if not same:
            continue
        big, small = (a, b) if counts[a.id] >= counts[b.id] else (b, a)
        db.execute(update(SpeechAct).where(SpeechAct.subject_id == small.id).values(subject_id=big.id))
        n_big, n_small = counts[big.id], counts[small.id]
        merged = [(x * n_big + y * n_small) / max(1, n_big + n_small) for x, y in zip(vecs[big.id], vecs[small.id])]
        vecs[big.id], counts[big.id] = merged, n_big + n_small
        big.embedding = pack(merged)
        db.execute(delete(DecisionEvent).where(DecisionEvent.subject_id == small.id))
        db.execute(delete(SubjectPosition).where(SubjectPosition.subject_id == small.id))
        # прочие ссылки на удаляемый предмет переходят на больший: строки сравнения с прежним контуром (пересчитаются) и «заменён предметом»
        db.execute(update(MemoryShadowReview).where(MemoryShadowReview.subject_id == small.id).values(subject_id=big.id))
        db.execute(update(Subject).where(Subject.superseded_by == small.id).values(superseded_by=big.id))
        db.delete(small)
        gone.add(small.id)
        stats["merged"] += 1
        db.flush()
    db.commit()
    log_event(logger, "subjects.merged", project=project_id, **stats)
    return stats
