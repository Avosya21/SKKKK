"""Переизвлечь требования проекта по новой схеме (docs/plans/REQUIREMENTS-PLAN.md, этап 9). Без --apply — только отчёт.

    venv\\Scripts\\python scripts\\reextract_project.py --project 18            # что будет сделано
    venv\\Scripts\\python scripts\\reextract_project.py --project 18 --apply    # поставить в очередь воркера

Работает воркер (одна GPU): каждый готовый материал — фоновая задача: извлечение v2 с карточкой проекта и оглавлением
созвона, перенос ручных решений, граф; затем проверки требований, темы, актуальность, конфликты. Воркер должен быть
перезапущен после обновления кода. Отчёт о перенесённых решениях: data/projects/<id>/project_state/reextract_human_transfer.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="поставить задачи в очередь (без флага — только отчёт)")
    args = ap.parse_args()

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.services.recorded_at import material_date
    from app.app.services.requirements_reextract import human_decisions_count, project_materials, queue_project_reextract

    settings = get_settings()
    db = SessionLocal()
    try:
        arts = project_materials(db, args.project)
        no_date = [a.original_filename for a in arts if material_date(a)[0] is None]
        print(json.dumps({"project": args.project, "materials": len(arts), "human_decisions": human_decisions_count(db, args.project),
                          "materials_without_recorded_date": no_date}, ensure_ascii=False, indent=2))
        if no_date:
            print("у этих материалов нет даты записи: замены «позже отказались» по ним не применятся (А4) — поставьте даты")
        if not args.apply:
            print("только отчёт; для постановки в очередь добавьте --apply")
            return 0
        print(json.dumps(queue_project_reextract(db, settings, args.project), ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
