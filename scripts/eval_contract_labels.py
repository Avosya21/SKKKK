"""Точность разбора требований по решениям людей (docs/plans/PORTAL-ACCURACY-MANAGER-PLAN.md, Ш1).

Только чтение, модель не вызывается: сводит решения людей (подтвердил / отклонил / сменил статус / «Вернуть решение модели»)
и выборочную проверку («Верно» / «Неверно») с тем, что извлекла и решила модель. Меньше 20 пунктов — «мало данных».

    venv\\Scripts\\python scripts\\eval_contract_labels.py              # все проекты
    venv\\Scripts\\python scripts\\eval_contract_labels.py --project 34

Результат — data/eval/contract_labels_<дата>.json (вне git: в нём только числа, но каталог общий с ответами чата).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _cell(m: dict) -> str:
    return f"{m['good']}/{m['n']}" + (f" ({round(m['share'] * 100)} %)" if m["share"] is not None else (" мало" if m["n"] else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=int, action="append", default=[])
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "eval")
    args = ap.parse_args()

    from sqlalchemy import select

    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.services.contract_quality import project_quality

    with SessionLocal() as db:
        ids = args.project or list(db.scalars(select(Project.id).where(Project.is_active.is_(True)).order_by(Project.id)).all())
        rows = [project_quality(db, pid) for pid in ids]

    print(f"{'проект':>7} {'пунктов':>8} {'извлечение верно':>18} {'модель права':>16} {'конфликты':>12} {'статус исправлен':>17} {'вернули скрытых':>16} {'выборка':>8}")
    for q in rows:
        rel = q["relevance"]
        print(f"{q['project_id']:>7} {q['items']:>8} {_cell(q['extraction']['all']):>18} {_cell(q['model_agreement']['all']):>16} "
              f"{_cell(q['conflicts']):>12} {q['status_changed_by_human']:>17} {str(rel['returned_by_human']) + '/' + str(rel['hidden']):>16} {q['spot_checked']:>8}")
    total_n = sum(q["model_agreement"]["all"]["n"] for q in rows)
    total_good = sum(q["model_agreement"]["all"]["good"] for q in rows)
    print(f"\nВсего пунктов, где решение модели можно сверить с человеком: {total_n}, модель права: {total_good}")

    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / f"contract_labels_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "projects": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Записано: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
