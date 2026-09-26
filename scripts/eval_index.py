"""Оценка индекса проекта на золотом наборе вопросов (docs/eval/index_questions.json).

  venv\\Scripts\\python.exe scripts\\eval_index.py               # прогон, таблица по группам
  venv\\Scripts\\python.exe scripts\\eval_index.py --validate    # только проверка набора (файлы существуют)
  venv\\Scripts\\python.exe scripts\\eval_index.py --min-recall5 0.85   # код выхода 1, если ниже порога
  venv\\Scripts\\python.exe scripts\\eval_index.py --show-misses        # какие вопросы не найдены

Метрики: recall@1/5/10 (нужный файл среди первых N попаданий), MRR. Попадание засчитывается по пути файла;
если у вопроса указан `symbol`, он должен встретиться в тексте попадания (при recall@5 — среди первых пяти).
Результат сохраняется в data/eval/index_eval_<время>.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

QUESTIONS = ROOT / "docs" / "eval" / "index_questions.json"
HOLDOUT = ROOT / "docs" / "eval" / "index_questions_holdout.json"  # контрольный набор: на нём веса не подбирались
OUT_DIR = ROOT / "data" / "eval"


def load_questions(path: Path | None = None) -> list[dict]:
    return json.loads((path or QUESTIONS).read_text(encoding="utf-8"))


def validate(questions: list[dict]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for item in questions:
        if item["id"] in seen:
            problems.append(f"{item['id']}: повторяющийся id")
        seen.add(item["id"])
        if not item.get("expect"):
            problems.append(f"{item['id']}: пустой expect")
        for path in item.get("expect", []):
            if not (ROOT / path).exists():
                problems.append(f"{item['id']}: нет файла {path}")
    return problems


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def hit_paths(hit: dict) -> set[str]:
    """Пути файлов, к которым относится попадание (для старого и нового индекса)."""
    paths: set[str] = set()
    for key in ("path", "source"):
        value = hit.get(key)
        if value:
            paths.add(_norm(str(value)))
    if hit.get("kind") == "manifest_node":
        match = re.search(r"path=(\S+)", hit.get("text", ""))
        if match:
            paths.add(_norm(match.group(1)))
    return paths


def rank_of(question: dict, hits: list[dict]) -> int | None:
    expected = {_norm(p) for p in question["expect"]}
    symbol = question.get("symbol")
    for position, hit in enumerate(hits, 1):
        if not (hit_paths(hit) & expected):
            continue
        if symbol and symbol not in (hit.get("text", "") + str(hit.get("symbol", "")) + str(hit.get("title", ""))):
            continue
        return position
    return None


def run(limit: int = 10, *, use_vectors: bool = True, questions_path: Path | None = None, rerank: bool = False) -> list[dict]:
    from app.app.services.project_index import search as index_search  # noqa: E402
    from app.app.services.project_index import store  # noqa: E402

    conn = store.connect()
    results: list[dict] = []
    try:
        for item in load_questions(questions_path):
            started = time.perf_counter()
            found = index_search.search(item["q"], limit=limit, conn=conn, use_vectors=use_vectors, rerank=rerank)
            hits = found["hits"]
            elapsed = time.perf_counter() - started
            results.append({**item, "rank": rank_of(item, hits), "seconds": round(elapsed, 2), "confident": found["confident"],
                            "top": [f"{h['path']}:{h['line_start']}" for h in hits[:3]]})
    finally:
        conn.close()
    return results


def summarize(results: list[dict]) -> dict:
    def stats(rows: list[dict]) -> dict:
        n = len(rows) or 1
        return {
            "n": len(rows),
            "recall1": sum(1 for r in rows if r["rank"] and r["rank"] <= 1) / n,
            "recall5": sum(1 for r in rows if r["rank"] and r["rank"] <= 5) / n,
            "recall10": sum(1 for r in rows if r["rank"] and r["rank"] <= 10) / n,
            "mrr": sum(1 / r["rank"] for r in rows if r["rank"]) / n,
            "avg_seconds": sum(r["seconds"] for r in rows) / n,
        }

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in results:
        groups[row["group"]].append(row)
    return {"total": stats(results), "groups": {name: stats(rows) for name, rows in groups.items()}}


def print_report(summary: dict) -> None:
    print(f"{'группа':<16}{'n':>4}{'r@1':>7}{'r@5':>7}{'r@10':>7}{'MRR':>7}{'сек':>7}")
    rows = [("ВСЕГО", summary["total"])] + sorted(summary["groups"].items())
    for name, s in rows:
        print(f"{name:<16}{s['n']:>4}{s['recall1']:>7.0%}{s['recall5']:>7.0%}{s['recall10']:>7.0%}{s['mrr']:>7.2f}{s['avg_seconds']:>7.2f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--validate", action="store_true", help="только проверить набор вопросов")
    parser.add_argument("--min-recall5", type=float, default=None, help="порог recall@5; ниже — код выхода 1")
    parser.add_argument("--show-misses", action="store_true", help="напечатать вопросы без попадания в топ-5")
    parser.add_argument("--tag", default="", help="метка запуска (например before/after)")
    parser.add_argument("--holdout", action="store_true", help="контрольный набор (веса на нём не подбирались)")
    parser.add_argument("--rerank", action="store_true", help="переранжировать верх выдачи локальной LLM (нужен LM Studio)")
    parser.add_argument("--no-vector", action="store_true", help="только точный поиск (без Ollama)")
    args = parser.parse_args()

    qpath = HOLDOUT if args.holdout else None
    questions = load_questions(qpath)
    problems = validate(questions)
    if problems:
        print("Набор вопросов некорректен:")
        for line in problems:
            print("  -", line)
        return 2
    if args.validate:
        print(f"набор в порядке: {len(questions)} вопросов")
        return 0

    results = run(use_vectors=not args.no_vector, questions_path=qpath, rerank=args.rerank)
    summary = summarize(results)
    print_report(summary)
    if args.show_misses:
        print("\nНе найдено в топ-5:")
        for row in results:
            if not row["rank"] or row["rank"] > 5:
                print(f"  [{row['id']}] {row['q']}  (ожидали {row['expect'][0]}, ранг={row['rank']}, топ={row['top']})")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = f"index_eval_{stamp}{('_' + args.tag) if args.tag else ''}.json"
    (OUT_DIR / name).write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nсохранено: data/eval/{name}")

    if args.min_recall5 is not None and summary["total"]["recall5"] < args.min_recall5:
        print(f"recall@5 {summary['total']['recall5']:.0%} ниже порога {args.min_recall5:.0%}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
