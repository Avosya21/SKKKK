"""Оценка качества быстрой сводки ТЗ (пункт плана 2.3).

Только чтение: собирает текст через build_tz_fast_body и НЕ пишет ни файлы проекта, ни БД —
можно запускать на клиентском проекте (ПИЛОТ, id 18).

    venv\\Scripts\\python scripts\\eval_tz.py --project 18 --runs 3 --facts docs\\eval\\tz_facts_project18.json

Метрики: recall по эталонным фактам, число запрещённых утверждений (противоречий), стабильность
(совпадение набора найденных фактов и похожесть текстов между прогонами), время, покрытие выборки.
Код выхода 1, если recall ниже --min-recall, есть противоречия или прогон дольше --max-seconds.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from datetime import datetime
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fact_hit(fact: dict, text: str) -> bool:
    if "all" in fact:
        return all(re.search(p, text) for p in fact["all"])
    return any(re.search(p, text) for p in fact.get("any", []))


def evaluate_text(text: str, spec: dict) -> dict:
    hits = {f["id"]: fact_hit(f, text) for f in spec["facts"]}
    bad = [f["id"] for f in spec.get("forbidden", []) if re.search(f["pattern"], text)]
    return {"hits": hits, "recall": sum(hits.values()) / max(1, len(hits)), "contradictions": bad}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--facts", type=Path, required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--min-recall", type=float, default=0.0, help="порог recall (0 — только измерить)")
    ap.add_argument("--max-seconds", type=float, default=60.0)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "eval")
    ap.add_argument("--set", action="append", default=[], metavar="ИМЯ=ЗНАЧЕНИЕ",
                    help="временно переопределить константу services/tz.py (эксперименты), напр. FAST_MAX_ENTS=90")
    args = ap.parse_args()

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.services.tz import build_tz_fast_body

    from app.app.services import tz as tz_module

    for item in args.set:
        key, _, raw = item.partition("=")
        current = getattr(tz_module, key)
        setattr(tz_module, key, type(current)(raw))
        print(f"override {key}: {current} -> {getattr(tz_module, key)}")

    spec = json.loads(args.facts.read_text(encoding="utf-8"))
    settings = get_settings()
    runs: list[dict] = []
    texts: list[str] = []
    for i in range(args.runs):
        db = SessionLocal()
        try:
            started = time.time()
            body, stats = build_tz_fast_body(db, settings, args.project)
            seconds = time.time() - started
        finally:
            db.close()
        text = body or ""
        texts.append(text)
        result = evaluate_text(text, spec)
        result.update(run=i + 1, seconds=round(seconds, 1), chars=len(text), coverage=stats)
        runs.append(result)
        print(
            f"run {i + 1}: recall={result['recall']:.2f} contradictions={result['contradictions']} "
            f"{seconds:.1f}s chars={len(text)} coverage={stats}",
            flush=True,
        )

    same_hits = all(r["hits"] == runs[0]["hits"] for r in runs)
    similarity = [round(difflib.SequenceMatcher(None, a, b).ratio(), 3) for a, b in combinations(texts, 2)]
    missing = sorted(k for k, v in runs[0]["hits"].items() if not all(r["hits"][k] for r in runs))
    summary = {
        "project": args.project,
        "time": datetime.now().isoformat(timespec="seconds"),
        "recall_mean": round(sum(r["recall"] for r in runs) / len(runs), 3),
        "recall_min": min(r["recall"] for r in runs),
        "contradictions": sorted({c for r in runs for c in r["contradictions"]}),
        "stable_fact_set": same_hits,
        "text_similarity_pairs": similarity,
        "max_seconds": max(r["seconds"] for r in runs),
        "facts_not_always_found": missing,
        "runs": runs,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / f"tz_{args.project}_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "runs"}, ensure_ascii=False, indent=2))
    print("отчёт:", out)

    failed = (
        summary["recall_min"] < args.min_recall
        or summary["contradictions"]
        or summary["max_seconds"] > args.max_seconds
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
