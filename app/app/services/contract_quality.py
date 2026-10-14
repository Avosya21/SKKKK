"""Точность разбора требований по решениям людей и выборочная проверка (docs/plans/PORTAL-ACCURACY-MANAGER-PLAN.md, Ш1).

Модель не вызывается: разметка — то, что люди уже сделали в портале. Решение человека по пункту (``value_json.human_review``:
подтвердил, сменил статус — пункт верный; отклонил — неверный; «Вернуть решение модели» — модель ошиблась) и отметка
выборочной проверки (``value_json.spot_check``: «Верно» / «Неверно»). Выборочная отметка — не подтверждение: она не меняет
``needs_review``, ``is_active`` и ``human_review``, пункт остаётся в «Ждут вас» и при переизвлечении не переносится.

Каждое число идёт с размером выборки; меньше ``MIN_SAMPLE`` — «мало данных», доля не считается.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app.models.artifact import Artifact
from app.app.models.entity import Entity, Link, Resolution

MIN_SAMPLE = 20
KINDS = ("decision", "task", "open_question")
SPOT_VERDICTS = ("ok", "wrong")
SPOT_WEEKLY = 10
HIDDEN_RELEVANCE = ("superseded", "duplicate")


def _value(ent: Entity) -> dict[str, Any]:
    try:
        value = json.loads(ent.value_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def human_label(value: dict[str, Any]) -> str | None:
    """«ok» / «wrong» по решению человека или выборочной отметке; None — человек пункт не оценивал."""
    action = (value.get("human_review") or {}).get("action")
    if action == "reject":
        return "wrong"
    if action in ("confirm", "status"):
        return "ok"
    verdict = (value.get("spot_check") or {}).get("verdict")
    return verdict if verdict in SPOT_VERDICTS else None


def _metric(n: int, good: int) -> dict[str, Any]:
    enough = n >= MIN_SAMPLE
    return {"n": n, "good": good, "share": round(good / n, 3) if enough and n else None, "enough": enough}


def _project_entities(db: Session, project_id: int) -> list[Entity]:
    return list(db.scalars(
        select(Entity).join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == project_id, Entity.entity_type.in_(KINDS))
    ).all())


def _conflicts(db: Session, project_id: int) -> dict[str, Any]:
    """Конфликты, которые решил человек, против вердикта модели по той же паре."""
    rows = db.execute(
        select(Link, Resolution).join(Resolution, Resolution.link_id == Link.id)
        .where(Link.project_id == project_id, Resolution.resolved_by.is_not(None), Link.model_review.is_not(None))
    ).all()
    n = good = 0
    for link, res in rows:
        try:
            review = json.loads(link.model_review or "{}")
        except ValueError:
            continue
        verdict = review.get("verdict")
        if verdict == "unsure":
            continue
        winner = review.get("newer") if verdict == "newer_wins" else review.get("older") if verdict == "older_wins" else None
        n += 1
        good += int(winner is not None and winner == res.winning_entity_id)
    return _metric(n, good)


def project_quality(db: Session, project_id: int) -> dict[str, Any]:
    ents = _project_entities(db, project_id)
    extraction = {k: [0, 0] for k in KINDS}
    model = {k: [0, 0] for k in KINDS}
    status_changed = hidden = returned = spot = 0
    for ent in ents:
        value = _value(ent)
        kind = ent.entity_type
        label = human_label(value)
        if label is not None:
            extraction[kind][0] += 1
            extraction[kind][1] += label == "ok"
        if value.get("spot_check"):
            spot += 1
        review = value.get("model_review") if isinstance(value.get("model_review"), dict) else None
        if review and review.get("verdict") in ("confirm", "reject"):
            if review.get("undone"):
                model[kind][0] += 1  # человек отменил решение модели — модель ошиблась
            elif label is not None:
                model[kind][0] += 1
                model[kind][1] += (review["verdict"] == "confirm") == (label == "ok")
        if value.get("human_status"):
            status_changed += 1
        rel = value.get("relevance") if isinstance(value.get("relevance"), dict) else {}
        if rel.get("state") in HIDDEN_RELEVANCE:
            hidden += 1
            returned += bool(rel.get("undone"))
    total_model = [sum(v[0] for v in model.values()), sum(v[1] for v in model.values())]
    total_extr = [sum(v[0] for v in extraction.values()), sum(v[1] for v in extraction.values())]
    return {
        "project_id": project_id,
        "items": len(ents),
        "extraction": {k: _metric(*v) for k, v in extraction.items()} | {"all": _metric(*total_extr)},
        "model_agreement": {k: _metric(*v) for k, v in model.items()} | {"all": _metric(*total_model)},
        "status_changed_by_human": status_changed,
        "relevance": {"hidden": hidden, "returned_by_human": returned},
        "conflicts": _conflicts(db, project_id),
        "spot_checked": spot,
        "min_sample": MIN_SAMPLE,
    }


def summary_line(quality: dict[str, Any]) -> str:
    """Строка для карточки «Подтверждение требований»."""
    m = quality["model_agreement"]["all"]
    if m["enough"]:
        return f"Модель права в {m['good']} из {m['n']} пунктов, проверенных людьми ({round(m['share'] * 100)} %)."
    return (f"Мало данных для оценки модели: люди проверили {m['n']} из {quality['min_sample']} нужных пунктов. "
            "Помогает вкладка «Выборочная проверка».")


# ---------- выборочная проверка ----------

def week_key(now: datetime | None = None) -> str:
    y, w, _ = (now or datetime.now(timezone.utc)).isocalendar()
    return f"{y}-W{w:02d}"


def _in_week(at: str | None, key: str) -> bool:
    try:
        return bool(at) and week_key(datetime.fromisoformat(str(at))) == key
    except ValueError:
        return False


def spot_sample(items: list[dict[str, Any]], spot_by_entity: dict[int, dict[str, Any]], project_id: int, *,
                now: datetime | None = None, size: int = SPOT_WEEKLY) -> tuple[list[dict[str, Any]], int]:
    """Пункты недели: ``size`` штук по порядку хеша (проект, неделя, сущность) — набор не меняется от перезагрузки.

    В пул входят пункты без отметки и отмеченные на этой неделе; показываются ещё не отмеченные. Возвращает (пункты, отмечено за неделю)."""
    key = week_key(now)
    pool = []
    for r in items:
        eid = r.get("source_entity_id")
        if not eid:
            continue
        spot = spot_by_entity.get(eid)
        if spot and not _in_week(spot.get("at"), key):
            continue
        rank = hashlib.sha1(f"{project_id}:{key}:{eid}".encode()).hexdigest()
        pool.append((rank, r, bool(spot)))
    pool.sort(key=lambda x: x[0])
    week = pool[:size]
    return [r for _rank, r, done in week if not done], sum(1 for *_x, done in week if done)


def record_spot(ent: Entity, *, verdict: str, reason: str | None, user) -> dict[str, Any]:
    if verdict not in SPOT_VERDICTS:
        raise ValueError("verdict: ok | wrong")
    value = _value(ent)
    spot = {"verdict": verdict, "by": user.name, "user_id": user.id, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if reason and reason.strip():
        spot["reason"] = reason.strip()[:500]
    value["spot_check"] = spot
    ent.value_json = json.dumps(value, ensure_ascii=False)
    return spot
