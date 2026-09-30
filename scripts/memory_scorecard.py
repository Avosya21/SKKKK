"""Табло памяти решений (docs/plans/DECISION-MEMORY-PLAN.md, §4.3): числа, по которым видно, приближаемся ли мы к цели.

    venv\\Scripts\\python scripts\\memory_scorecard.py --project 18 --stage E0            # посчитать и добавить строку в табло
    venv\\Scripts\\python scripts\\memory_scorecard.py --project 18 --stage E0 --no-save  # только показать

Эталон и правила лежат в ``data/eval`` (вне git: там имена, цитаты и суммы проекта):
- ``memory_gold.csv``   — размеченные ленты предметов (события с цитатами и «ИТОГ» по каждому предмету);
- ``memory_rules.json`` — правила M2: по каким словам найти пункты предмета и какие состояния считаются верными.
Табло — ``data/eval/memory_scorecard.csv`` (одна строка на прогон).

Метрики базовой линии (до журнала событий):
- M1 — доля пунктов контракта с цитатой источника;
- M2 — доля правил эталона, по которым состояние пунктов системы верное;
- M3~ — грубый показатель: доля эталонных событий, у которых есть пункт с тем же якорем (сообщение или реплика).
  Вопросы, ответы и «сделал» пунктами не становятся, поэтому до речевых актов (этап Э2) число низкое.
M4–M6 появятся вместе с этапами Э2–Э6.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EVAL = ROOT / "data" / "eval"
GOLD = EVAL / "memory_gold.csv"
RULES = EVAL / "memory_rules.json"
BOARD = EVAL / "memory_scorecard.csv"
HEADER = ["дата", "этап", "проект", "промпт", "модель", "M1_цитата", "M2_итог", "M3_грубо", "M2_подробно"]


def _state(value: dict, active: bool) -> str:
    review = value.get("model_review") or {}
    rel = value.get("relevance") or {}
    rel_state = rel.get("state") if not rel.get("undone") else None
    if not active:
        if rel_state in ("superseded", "duplicate"):
            return rel_state
        if review.get("verdict") == "reject" and not (review.get("undone")):
            return "rejected"
        return "rejected"
    if rel_state in ("superseded", "duplicate", "done"):
        return rel_state
    if value.get("status") == "done":
        return "done"
    return "current"


def _gold_events() -> list[dict]:
    if not GOLD.exists():
        return []
    with GOLD.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f, delimiter=";") if r.get("акт") != "ИТОГ" and r.get("источник")]


def _anchors(db, project_id: int) -> set[tuple[int, str, int]]:
    """Якоря пунктов: (номер материала, 'M'|'T', номер сообщения или начало реплики в секундах)."""
    from sqlalchemy import select

    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity

    out: set[tuple[int, str, int]] = set()
    rows = db.execute(select(Entity, Artifact).join(Artifact, Artifact.id == Entity.artifact_id)
                      .where(Artifact.project_id == project_id, Entity.entity_type.in_(("decision", "task", "open_question")))).all()
    for ent, art in rows:
        try:
            value = json.loads(ent.value_json or "{}")
        except ValueError:
            continue
        if value.get("message_id") is not None:
            out.add((art.id, "M", int(value["message_id"])))
        if value.get("start_sec") is not None:
            out.add((art.id, "T", int(float(value["start_sec"]))))
    return out


def _turn_start(db, settings, art_id: int, index: int) -> int | None:
    from app.app.models.artifact import Artifact
    from app.app.services.transcript_turns import load_turns

    for t in load_turns(settings, db.get(Artifact, art_id)):
        if t.index == index and t.start_sec is not None:
            return int(t.start_sec)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--stage", default="E0")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.services import requirement_review as rr
    from app.app.services.entities import PROMPT_VERSION

    settings = get_settings()
    db = SessionLocal()
    try:
        counts = rr.review_list(db, settings, args.project, limit=1)["counts"]
        live = max(1, counts["total"])
        m1 = 1 - counts["unsourced"] / live

        rules = json.loads(RULES.read_text(encoding="utf-8")) if RULES.exists() else []
        ents = []
        for ent, _art in db.execute(select(Entity, Artifact).join(Artifact, Artifact.id == Entity.artifact_id)
                                    .where(Artifact.project_id == args.project, Entity.entity_type.in_(("decision", "task")))).all():
            value = json.loads(ent.value_json or "{}")
            text = str(value.get("text") or value.get("description") or "")
            ents.append((text, _state(value, bool(ent.is_active))))
        passed, details = 0, []
        for rule in rules:
            pat = re.compile(rule["pattern"], re.I)
            states = [st for text, st in ents if pat.search(text)]
            live_states = [st for st in states if st not in ("rejected", "duplicate")] or states
            got = live_states or ["none"]
            if rule.get("positive"):
                ok = any(st in rule["accept"] for st in got)  # нужен хотя бы один пункт в верном состоянии
            else:
                ok = all(st in rule["accept"] for st in got)  # ни один не должен остаться «в силе»
            passed += ok
            details.append(f"{rule['subject']} {'OK ' if ok else 'НЕТ'} {rule['note']}: {sorted(set(got))}")
        m2 = passed / len(rules) if rules else None

        gold = _gold_events()
        anchors = _anchors(db, args.project)
        found = 0
        for ev in gold:
            m = re.fullmatch(r"a(\d+):([MT])(\d+)", ev["источник"])
            if not m:
                continue
            art_id, kind, num = int(m.group(1)), m.group(2), int(m.group(3))
            if kind == "T":
                start = _turn_start(db, settings, art_id, num)
                hit = start is not None and any(a[0] == art_id and a[1] == "T" and abs(a[2] - start) <= 1 for a in anchors)
            else:
                hit = (art_id, "M", num) in anchors
            found += hit
        m3 = found / len(gold) if gold else None
    finally:
        db.close()

    row = [datetime.now().strftime("%Y-%m-%d %H:%M"), args.stage, args.project, PROMPT_VERSION, settings.lmstudio_model,
           f"{m1:.2f}", "" if m2 is None else f"{m2:.2f}", "" if m3 is None else f"{m3:.2f}", " | ".join(details)]
    print(f"M1 доля пунктов с цитатой источника: {m1:.0%} (без цитаты {counts['unsourced']} из {counts['total']})")
    print(f"M2 верных итогов по правилам эталона: {'нет правил' if m2 is None else f'{passed} из {len(rules)} ({m2:.0%})'}")
    for d in details:
        print("   ", d)
    print(f"M3~ эталонных событий с пунктом на том же якоре: {'нет эталона' if m3 is None else f'{found} из {len(gold)} ({m3:.0%})'}")
    if not args.no_save:
        new = not BOARD.exists()
        with BOARD.open("a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow(HEADER)
            w.writerow(row)
        print(f"Строка добавлена: {BOARD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
