"""Табло памяти решений: числа, по которым видно, приближаемся ли мы к цели (docs/plans/DECISION-MEMORY-PLAN.md, §4.3).

    venv\\Scripts\\python scripts\\memory_scorecard.py --project 18 --stage E0            # посчитать и добавить строку в табло
    venv\\Scripts\\python scripts\\memory_scorecard.py --project 18 --no-save            # только показать
    venv\\Scripts\\python scripts\\memory_scorecard.py --project 18 --export-people      # лист людей для M8 из реестра проекта

Расчёт — в ``app/app/services/memory_scorecard.py`` (то же считает страница ``/admin/memory``). Эталон и история лежат в
``data/eval`` (вне git: имена, цитаты и суммы проекта): ``memory_gold.csv``, ``memory_rules.json``, ``memory_people.csv``,
``memory_scorecard.csv``. Модель не вызывается.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STATUS_MARK = {"ok": "ok  ", "warn": "ВНИМ", "fail": "НЕТ ", "manual": "руками"}


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--stage", default="ручной")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--export-people", action="store_true", help="записать лист людей из реестра проекта (эталон для M8) и выйти")
    args = ap.parse_args(argv)

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.services import memory_scorecard as sc

    settings = get_settings()
    db = SessionLocal()
    try:
        if args.export_people:
            print(f"Лист людей записан: {sc.export_people_sheet(db, settings, args.project)} чел. ({sc.eval_dir(settings) / 'memory_people.csv'}). Проверьте его глазами.")
            return 0
        result = sc.compute(db, settings, args.project) if args.no_save else sc.run(db, settings, args.project, stage=args.stage)
    finally:
        db.close()
    m, d = result["metrics"], result["details"]
    print(f"M1 пунктов с цитатой источника: {_pct(m['M1'])} (без цитаты {d['unsourced']} из {d['total']})")
    print(f"M2 верных итогов по правилам эталона: {_pct(m['M2'])}")
    for item in d["m2"]:
        print(f"    {item['subject']} {'OK ' if item['ok'] else 'НЕТ'} {item['note']}: {item['got']}")
    print(f"M3~ эталонных событий с пунктом на том же якоре: {_pct(m['M3'])} ({d['m3'][0]} из {d['m3'][1]})")
    print(f"M7 людей из эталона с известной стороной в реестре: {_pct(m['M7'])}" + (f"; не найдены: {', '.join(d['m7_missing'])}" if d["m7_missing"] else ""))
    print(f"M8 верная сторона источника в контракте: {_pct(m['M8'])} (проверено событий {d['m8_checked']})")
    print(f"M9 пунктов документов с абзацем-источником: {_pct(m['M9'])} ({d['m9'][0]} из {d['m9'][1]})")
    a = d["acts"]
    print(f"A1 полнота речевых актов: {_pct(m['A1'])} ({a['found']} из {a['total']}); A2 верный вид акта: {_pct(m['A2'])} ({a['right']} из {a['found']})")
    j = result["journal_m2"]
    if j["value"] is not None:
        print(f"M2 по журналу решений (предметы): {_pct(j['value'])}")
        for x in j["details"]:
            print(f"    {x['subject']} {'OK ' if x['ok'] else 'НЕТ'} {x['note']}: {x['got']}")
    for w in a["wrong"]:
        print(f"    вид не совпал {w['source']}: эталон «{w['gold']}», система {w['got']}")
    for w in d["m8_wrong"]:
        print(f"    расхождение {w['source']} {w['author']}: ждали {w['expected']}, в контракте {w['got']}")
    print("M4–M6: появятся вместе с речевыми актами, журналом событий и ответами чата.")
    print("\nЧек-лист приёмки платформы:")
    for item in result["checklist"]:
        print(f"  [{STATUS_MARK[item['status']]}] {item['title']} — {item['detail']}")
    if not args.no_save:
        print(f"\nСтрока добавлена: {sc.eval_dir(settings) / 'memory_scorecard.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
