"""Замер автопроверки требований (план docs/plans/AUTO-REVIEW-PLAN.md, §9).

Прогон — на ВРЕМЕННОЙ КОПИИ базы (sqlite backup): вердикты пишутся в копию, боевая БД и очередь не трогаются.
Файлы проекта читаются только на чтение. Модель — та же, что у воркера (LM Studio / Cursor из .env).

    # 1) прогнать проверку в тени и получить таблицу для разметки (скорость — в терминале)
    venv\\Scripts\\python scripts\\eval_auto_review.py --project 35 --sample 60
    # 2) заполнить в CSV колонку «верно (да/нет)» и посчитать метрики
    venv\\Scripts\\python scripts\\eval_auto_review.py --score data\\eval\\auto_review_35_....csv

Метрики (пороги для live — §9 плана): ложное подтверждение ≤ 5 %, ложное отклонение ≤ 2 %, доля «не уверен» ≤ 25 %.

Эталон конфликтов (docs/plans/CONFLICT-AUTONOMY-PLAN.md) — один постоянный файл data/eval/conflicts_gold.csv:
    # 1) добавить конфликты проекта в эталон и разметить столбец «в силе (эталон: A/B/оба)»
    venv\\Scripts\\python scripts\\eval_auto_review.py --export-gold --project 30
    # 2) контрольный прогон (обязателен при смене модели, промпта или извлечения); код 0 — можно live
    venv\\Scripts\\python scripts\\eval_auto_review.py --gold
    # 3) пополнение — только парами, где человек нажал «Вернуть»
    venv\\Scripts\\python scripts\\eval_auto_review.py --export-gold --only-undone --project 30
Пороги live: неверных победителей 0, размечено ≥ 20, «не уверена» ≤ 30 %.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ANSWER_COL = "верно (да/нет)"
LIMITS = {"false_confirm": 0.05, "false_reject": 0.02, "unsure_share": 0.25}


def snapshot_db(src: Path, dst: Path) -> None:
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    with target:
        source.backup(target)
    source.close()
    target.close()


def _live_queue_busy(real_db: Path) -> int:
    conn = sqlite3.connect(f"file:{real_db.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("select count(*) from jobs where status in ('queued','running')").fetchone()[0]
    finally:
        conn.close()


def run(args: argparse.Namespace) -> int:
    real_db = ROOT / "data" / "app.db"
    busy = _live_queue_busy(real_db)
    if busy:
        print(f"в боевой очереди {busy} задач — замер не запускаю: одна GPU, расшифровке нельзя мешать", file=sys.stderr)
        return 2
    tmp_dir = Path(tempfile.mkdtemp(prefix="eval_review_"))
    copy = tmp_dir / "app_copy.db"
    snapshot_db(real_db, copy)
    os.environ["DATABASE_URL"] = f"sqlite:///{copy.as_posix()}"

    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal, apply_schema_migrations
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.services.review_requirements import PROMPT_VERSION, review_artifact_requirements

    settings = get_settings()
    assert copy.as_posix() in settings.database_url, "замер должен работать на копии БД"
    # Замер не управляет VRAM: модель должна быть уже загружена (её грузит воркер), сам скрипт ничего не грузит и не выгружает.
    import app.app.services.gpu_phase as gpu_phase
    from app.app.services.llm import lmstudio_model_loaded, lmstudio_models, provider_uses_lmstudio

    if provider_uses_lmstudio(settings) and not lmstudio_model_loaded(settings.lmstudio_model, lmstudio_models(settings)):
        print(f"в LM Studio не загружена {settings.lmstudio_model} — замер не запускаю", file=sys.stderr)
        return 2
    gpu_phase.prepare_for_llm = lambda *_a, **_k: None
    apply_schema_migrations()
    db = SessionLocal()
    arts = db.scalars(
        select(Artifact).where(Artifact.project_id == args.project, Artifact.status == "done").order_by(Artifact.id)
    ).all()
    if args.max_artifacts:
        arts = arts[: args.max_artifacts]
    if args.scope == "conflicts":
        return _run_conflicts(args, db, settings, arts)
    totals: dict[str, float] = {}
    started = time.monotonic()
    for art in arts:
        t0 = time.monotonic()
        stats = review_artifact_requirements(db, settings, art, "shadow")
        db.commit()
        for key, value in stats.items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value
        print(f"{art.artifact_id} {art.original_filename[:40]:40} {json.dumps(stats, ensure_ascii=False)} {time.monotonic() - t0:.1f}s", flush=True)
    elapsed = time.monotonic() - started

    rows = []
    for ent, art in db.execute(
        select(Entity, Artifact).join(Artifact, Artifact.id == Entity.artifact_id)
        .where(Artifact.project_id == args.project, Entity.value_json.like('%"model_review"%'))
    ).all():
        value = json.loads(ent.value_json)
        review = value.get("model_review") or {}
        if review.get("prompt_version") != PROMPT_VERSION:
            continue
        rows.append({
            "entity_id": ent.id, "файл": art.original_filename, "тип": ent.entity_type,
            "текст": value.get("text") or value.get("description") or "",
            "вердикт модели": review.get("verdict"), "уверенность": review.get("confidence"),
            "причина": review.get("reason"), "правило": review.get("rule") or "",
            "утвердил (модель)": ", ".join(d.get("name", "") for d in review.get("decided_by") or []),
            "сказал": value.get("speaker") or "", "время, с": value.get("start_sec") or "",
            ANSWER_COL: "",
        })
    random.Random(args.seed).shuffle(rows)
    sample = rows[: args.sample]
    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / f"auto_review_{args.project}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(sample[0].keys()) if sample else [ANSWER_COL], delimiter=";")
        writer.writeheader()
        writer.writerows(sample)
    db.close()
    calls = totals.get("llm_calls", 0) or 0
    print(json.dumps({
        "project": args.project, "artifacts": len(arts), "prompt_version": PROMPT_VERSION,
        "entities": int(totals.get("total", 0)), "confirm": int(totals.get("confirm", 0)), "reject": int(totals.get("reject", 0)),
        "unsure": int(totals.get("unsure", 0)), "no_source": int(totals.get("no_source", 0)), "duplicate": int(totals.get("duplicate", 0)),
        "llm_calls": int(calls), "llm_errors": int(totals.get("llm_errors", 0)),
        "seconds_total": round(elapsed, 1), "seconds_per_llm_call": round(elapsed / calls, 1) if calls else None,
        "seconds_per_entity": round(elapsed / max(1, totals.get("total", 0)), 1),
    }, ensure_ascii=False, indent=2))
    print("таблица для разметки:", out)
    return 0


def _run_conflicts(args: argparse.Namespace, db, settings, arts) -> int:
    """Конфликты проекта в тени на копии БД: таблица «первое / второе / вердикт / причина» для разметки."""
    from sqlalchemy import select

    from app.app.models.entity import Link
    from app.app.services.review_conflicts import PROMPT_VERSION, conflict_rows, review_artifact_conflicts

    totals: dict[str, float] = {}
    started = time.monotonic()
    for art in arts:
        stats = review_artifact_conflicts(db, settings, art, "shadow")
        db.commit()
        for key, value in stats.items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value
        if stats.get("total"):
            print(f"{art.artifact_id} {json.dumps(stats, ensure_ascii=False)}", flush=True)
    elapsed = time.monotonic() - started
    rows = []
    for c in conflict_rows(db, args.project, view="pending"):
        review = c.get("model_review") or {}
        if review.get("prompt_version") != PROMPT_VERSION:
            continue
        rows.append({"link_id": c["link_id"], "первое": (c["source"] or {}).get("line"), "второе": (c["target"] or {}).get("line"),
                     "вердикт модели": review.get("verdict"), "уверенность": review.get("confidence"), "причина": review.get("reason"),
                     "раньше (id)": review.get("older"), "позже (id)": review.get("newer"), ANSWER_COL: ""})
    random.Random(args.seed).shuffle(rows)
    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / f"auto_review_conflicts_{args.project}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [ANSWER_COL], delimiter=";")
        writer.writeheader()
        writer.writerows(rows[: args.sample])
    db.close()
    calls = totals.get("llm_calls", 0) or 0
    print(json.dumps({"project": args.project, "prompt_version": PROMPT_VERSION, **{k: int(v) for k, v in totals.items() if k != "seconds"},
                      "seconds_total": round(elapsed, 1), "seconds_per_llm_call": round(elapsed / calls, 1) if calls else None},
                     ensure_ascii=False, indent=2))
    print("таблица для разметки:", out)
    return 0


# Эталон конфликтов (CONFLICT-AUTONOMY-PLAN этапы 2 и 7): ОДИН постоянный файл, не на проект. Размечается один раз;
# дальше — контрольный прогон при смене модели, промпта или извлечения; пополняется только парами, где человек нажал «Вернуть».
GOLD_PATH = ROOT / "data" / "eval" / "conflicts_gold.csv"
GOLD_RELATION = "отношение (эталон)"
GOLD_IN_FORCE = "в силе (эталон: A/B/оба)"
GOLD_FIELDS = ["project", "older_id", "newer_id", "почему в наборе", "A (раньше)", "B (позже)", "источник A", "источник B",
               "вес A", "вес B", GOLD_RELATION, GOLD_IN_FORCE]
GOLD_LIMITS = {"wrong_winner": 0, "min_labeled": 20, "unsure_share": 0.30}
_IN_FORCE = {"a": "A", "а": "A", "b": "B", "в": "B", "оба": "both", "both": "both"}
_VERDICT_TO_FORCE = {"older_wins": "A", "newer_wins": "B", "not_conflict": "both"}


def _open_copy(check_llm: bool):
    """Сессия на временной копии боевой БД (пишем только в копию). None — если замер сейчас нельзя."""
    real_db = ROOT / "data" / "app.db"
    if check_llm and _live_queue_busy(real_db):
        print("в боевой очереди есть задачи — замер не запускаю: одна GPU, расшифровке нельзя мешать", file=sys.stderr)
        return None
    copy = Path(tempfile.mkdtemp(prefix="eval_review_")) / "app_copy.db"
    snapshot_db(real_db, copy)
    os.environ["DATABASE_URL"] = f"sqlite:///{copy.as_posix()}"
    from app.app.config import get_settings
    from app.app.db import SessionLocal, apply_schema_migrations

    settings = get_settings()
    assert copy.as_posix() in settings.database_url, "замер должен работать на копии БД"
    if check_llm:
        import app.app.services.gpu_phase as gpu_phase
        from app.app.services.llm import lmstudio_model_loaded, lmstudio_models, provider_uses_lmstudio

        if provider_uses_lmstudio(settings) and not lmstudio_model_loaded(settings.lmstudio_model, lmstudio_models(settings)):
            print(f"в LM Studio не загружена {settings.lmstudio_model} — замер не запускаю", file=sys.stderr)
            return None
        gpu_phase.prepare_for_llm = lambda *_a, **_k: None
    apply_schema_migrations()
    return SessionLocal(), settings


def _prepare_pair(db, settings, older_id: int, newer_id: int, project_id: int, cache: dict):
    """На копии: обе стороны включены, без решений и отметок человека — меряем модель и правила, а не историю."""
    import json as _json

    from sqlalchemy import or_, select

    from app.app.models.entity import Entity, Link, Resolution
    from app.app.services.review_conflicts import CONFLICT, _pair

    ents = [db.get(Entity, older_id), db.get(Entity, newer_id)]
    if any(e is None for e in ents):
        return None
    for ent in ents:
        ent.is_active = True
        value = _json.loads(ent.value_json or "{}")
        if isinstance(value, dict):
            value.pop("human_review", None)
            value.pop("model_review", None)
            ent.value_json = _json.dumps(value, ensure_ascii=False)
    link = db.scalar(select(Link).where(Link.link_type == CONFLICT, or_(
        (Link.source_entity_id == newer_id) & (Link.target_entity_id == older_id),
        (Link.source_entity_id == older_id) & (Link.target_entity_id == newer_id))))
    if link is None:
        link = Link(project_id=project_id, source_entity_id=newer_id, target_entity_id=older_id, link_type=CONFLICT, is_automatic=True)
        db.add(link)
        db.flush()
    for res in db.scalars(select(Resolution).where(Resolution.link_id == link.id)).all():
        db.delete(res)
    link.model_review = None
    db.flush()
    return _pair(db, settings, link, cache)


def export_gold(args: argparse.Namespace) -> int:
    """Добавить в эталон конфликты проекта (решения/задачи/факты), которых там ещё нет. Разметка — два последних столбца."""
    opened = _open_copy(check_llm=False)
    if opened is None:
        return 2
    db, settings = opened
    from sqlalchemy import select

    from app.app.models.entity import Link, Resolution
    from app.app.services.review_conflicts import CONFLICT, link_review

    rows = _read_gold(args.gold_file)
    seen = {(r["older_id"], r["newer_id"]) for r in rows} | {(r["newer_id"], r["older_id"]) for r in rows}
    added = 0
    cache: dict = {}
    for link in db.scalars(select(Link).where(Link.project_id == args.project, Link.link_type == CONFLICT).order_by(Link.id)).all():
        review = link_review(link) or {}
        res = db.scalar(select(Resolution).where(Resolution.link_id == link.id))
        why = ("человек отменил решение модели" if review.get("undone") else
               "решил человек" if res is not None and res.resolved_by is not None else "конфликт")
        if args.only_undone and why != "человек отменил решение модели":
            continue
        pair = _prepare_pair(db, settings, link.target_entity_id, link.source_entity_id, args.project, cache)
        if pair is None or not pair["material"]:
            continue
        key = (str(pair["older"]["id"]), str(pair["newer"]["id"]))
        if key in seen:
            continue
        seen.add(key)
        rows.append({"project": args.project, "older_id": key[0], "newer_id": key[1], "почему в наборе": why,
                     "A (раньше)": pair["older"]["line"], "B (позже)": pair["newer"]["line"],
                     "источник A": " | ".join(pair["older"]["source"])[:1500], "источник B": " | ".join(pair["newer"]["source"])[:1500],
                     "вес A": pair["older"]["weight"], "вес B": pair["newer"]["weight"], GOLD_RELATION: "", GOLD_IN_FORCE: ""})
        added += 1
    db.rollback()
    db.close()
    _write_gold(args.gold_file, rows)
    print(json.dumps({"project": args.project, "added": added, "total_in_gold": len(rows), "file": str(args.gold_file)}, ensure_ascii=False))
    print(f"разметьте «{GOLD_IN_FORCE}»: A — в силе раннее, B — позднее, оба — не противоречие; «{GOLD_RELATION}» — по желанию")
    return 0


def _read_gold(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [dict(r) for r in csv.DictReader(fh, delimiter=";")]


def _write_gold(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=GOLD_FIELDS, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def gold_eval(args: argparse.Namespace) -> int:
    """Контрольный прогон по эталону на копии БД: правила + модель, как в live, но без последствий."""
    labeled = [r for r in _read_gold(args.gold_file) if _IN_FORCE.get((r.get(GOLD_IN_FORCE) or "").strip().lower())]
    if args.project:
        labeled = [r for r in labeled if str(r.get("project")) == str(args.project)]
    if not labeled:
        print(f"в {args.gold_file} нет размеченных строк («{GOLD_IN_FORCE}»)", file=sys.stderr)
        return 2
    opened = _open_copy(check_llm=True)
    if opened is None:
        return 2
    db, settings = opened
    from app.app.services.auto_review import MODE_SHADOW
    from app.app.services.prompts import load_prompt_block
    from app.app.services.review_conflicts import PROMPT_PATH, PROMPT_VERSION, apply_conflict_verdict, ask_model, rule_before_model
    from app.app.services.review_requirements import _model_label

    system = load_prompt_block(PROMPT_PATH)
    model = _model_label(settings)
    cache: dict = {}
    report, started = [], time.monotonic()
    for row in labeled:
        expected = _IN_FORCE[(row[GOLD_IN_FORCE] or "").strip().lower()]
        pair = _prepare_pair(db, settings, int(row["older_id"]), int(row["newer_id"]), int(row["project"]), cache)
        if pair is None:
            report.append({**row, "итог": "нет в базе", "ошибка": ""})
            continue
        db.commit()
        decided = rule_before_model(pair)
        confidence, by = 1.0, (decided or {}).get("rule")
        if decided is None:
            try:
                decided, confidence = ask_model(settings, system, pair)
                by = model
            except Exception as exc:  # noqa: BLE001
                report.append({**row, "итог": "ошибка модели", "ошибка": str(exc)[:200]})
                continue
        verdict = apply_conflict_verdict(db, settings, pair, verdict=decided["verdict"], reason=decided["reason"], confidence=confidence,
                                         mode=MODE_SHADOW, model=by or model, rule=decided["rule"],
                                         reject_kind=decided["reject_kind"], relation=decided["relation"])
        db.commit()
        got = _VERDICT_TO_FORCE.get(verdict, "unsure")
        # Опасная ошибка — выключен пункт, который по эталону в силе: победитель не совпал или «оба» в силе.
        wrong = verdict in ("newer_wins", "older_wins") and got != expected
        report.append({**row, "итог": got, "правило/модель": by, "отношение (модель)": decided.get("relation") or "",
                       "причина": decided["reason"][:300], "ошибка": "НЕВЕРНЫЙ ПОБЕДИТЕЛЬ" if wrong else ""})
    db.close()
    evaluated = [r for r in report if r["итог"] in ("A", "B", "both", "unsure")]
    result = {
        "prompt_version": PROMPT_VERSION, "labeled": len(labeled), "evaluated": len(evaluated),
        "wrong_winner": sum(r["ошибка"] == "НЕВЕРНЫЙ ПОБЕДИТЕЛЬ" for r in report),
        "correct": sum(r["итог"] == _IN_FORCE[(r[GOLD_IN_FORCE] or "").strip().lower()] for r in evaluated),
        "unsure_share": round(sum(r["итог"] == "unsure" for r in evaluated) / max(1, len(evaluated)), 3),
        "by_rule": sum(str(r.get("правило/модель") or "").startswith("rule") for r in evaluated),
        "seconds": round(time.monotonic() - started, 1),
    }
    result["ready_for_live"] = (result["wrong_winner"] <= GOLD_LIMITS["wrong_winner"] and len(evaluated) >= GOLD_LIMITS["min_labeled"]
                                and result["unsure_share"] <= GOLD_LIMITS["unsure_share"])
    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / f"conflicts_gold_run_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        fields = GOLD_FIELDS + ["итог", "правило/модель", "отношение (модель)", "причина", "ошибка"]
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("построчно:", out)
    return 0 if result["ready_for_live"] else 1


def score(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh, delimiter=";")]
    marked = [r for r in rows if (r.get(ANSWER_COL) or "").strip().lower() in ("да", "нет", "yes", "no", "+", "-")]
    ok = lambda r: (r[ANSWER_COL] or "").strip().lower() in ("да", "yes", "+")  # noqa: E731
    confirms = [r for r in marked if r["вердикт модели"] == "confirm"]
    rejects = [r for r in marked if r["вердикт модели"] == "reject"]
    unsure = [r for r in rows if r["вердикт модели"] == "unsure"]
    result = {
        "rows": len(rows), "marked": len(marked),
        "false_confirm": round(sum(not ok(r) for r in confirms) / max(1, len(confirms)), 3),
        "false_reject": round(sum(not ok(r) for r in rejects) / max(1, len(rejects)), 3),
        "unsure_share": round(len(unsure) / max(1, len(rows)), 3),
    }
    result["ready_for_live"] = all(result[k] <= limit for k, limit in LIMITS.items()) and len(marked) >= min(30, len(rows))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready_for_live"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int)
    ap.add_argument("--sample", type=int, default=60)
    ap.add_argument("--max-artifacts", type=int, default=0)
    ap.add_argument("--seed", type=int, default=24)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "eval")
    ap.add_argument("--score", type=Path)
    ap.add_argument("--scope", choices=("requirements", "conflicts"), default="requirements")
    ap.add_argument("--export-gold", action="store_true", help="добавить конфликты проекта в эталон для разметки")
    ap.add_argument("--only-undone", action="store_true", help="с --export-gold: только пары, где человек нажал «Вернуть»")
    ap.add_argument("--gold", action="store_true", help="контрольный прогон по размеченному эталону конфликтов")
    ap.add_argument("--gold-file", type=Path, default=GOLD_PATH)
    args = ap.parse_args()
    if args.score:
        return score(args.score)
    if args.gold:
        return gold_eval(args)
    if args.export_gold:
        if not args.project:
            ap.error("--export-gold требует --project")
        return export_gold(args)
    if not args.project:
        ap.error("нужен --project или --score")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
