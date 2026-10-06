"""Оценка чата-ассистента (пункт плана 2.9).

Безопасно для клиентских проектов: работает на ВРЕМЕННОЙ КОПИИ базы (sqlite backup), поэтому сообщения чата
и журнал активности пишутся в копию, а не в боевую БД. Файлы проекта читаются только на чтение.

    venv\\Scripts\\python scripts\\eval_chat.py --project 18 --questions docs\\eval\\chat_questions_project18.json

Метрики: пустые ответы, доля ответов с источниками, попадание в ожидаемые факты, отказ на «ловушках»
(вопросы, ответа на которые в проекте нет), среднее время. Код выхода 1 при пустых ответах или провалах.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def snapshot_db(src: Path, dst: Path) -> None:
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    with target:
        source.backup(target)
    source.close()
    target.close()


def hit(question: dict, answer: str) -> bool:
    flags = re.IGNORECASE
    if "all" in question:
        return all(re.search(p, answer, flags) for p in question["all"])
    return any(re.search(p, answer, flags) for p in question.get("any", []))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--questions", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "eval")
    ap.add_argument("--memory-live", action="store_true", help="в копии БД включить память решений проекта (режим live): замер M6 по журналу решений")
    args = ap.parse_args()

    real_db = ROOT / "data" / "app.db"
    tmp_dir = Path(tempfile.mkdtemp(prefix="eval_chat_"))
    copy = tmp_dir / "app_copy.db"
    snapshot_db(real_db, copy)
    os.environ["DATABASE_URL"] = f"sqlite:///{copy.as_posix()}"

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.user import User
    from app.app.services.chat import answer_chat

    settings = get_settings()
    assert str(copy.as_posix()) in settings.database_url, "eval должен работать на копии БД"
    spec = json.loads(args.questions.read_text(encoding="utf-8"))
    db = SessionLocal()
    if args.memory_live:
        from app.app.models.project import Project
        from app.app.services import memory_mode

        memory_mode.set_mode(db, db.get(Project, args.project), "live")  # только в копии БД
    user = db.query(User).order_by(User.id).first()
    rows: list[dict] = []
    for item in spec["questions"]:
        started = time.time()
        try:
            result = answer_chat(db, settings, args.project, user.id, item["q"])
            answer, sources = result.get("answer") or "", result.get("sources") or []
            error = None
        except Exception as exc:  # noqa: BLE001
            answer, sources, error = "", [], repr(exc)[:200]
        seconds = round(time.time() - started, 1)
        if item.get("trap"):
            ok = bool(answer.strip()) and hit({"any": spec["refusal_patterns"]}, answer)
        else:
            ok = hit(item, answer)
        rows.append(
            {"id": item["id"], "trap": bool(item.get("trap")), "empty": not answer.strip(), "sources": len(sources),
             "ok": ok, "seconds": seconds, "error": error, "answer": answer[:400]}
        )
        print(f"{item['id']:>14} ok={ok!s:5} empty={not answer.strip()!s:5} src={len(sources)} {seconds}s", flush=True)
    db.close()

    normal = [r for r in rows if not r["trap"]]
    traps = [r for r in rows if r["trap"]]
    summary = {
        "project": args.project,
        "time": datetime.now().isoformat(timespec="seconds"),
        "questions": len(rows),
        "empty_answers": sum(r["empty"] for r in rows),
        "errors": sum(bool(r["error"]) for r in rows),
        "with_sources_share": round(sum(r["sources"] > 0 for r in normal) / max(1, len(normal)), 3),
        "fact_hit_share": round(sum(r["ok"] for r in normal) / max(1, len(normal)), 3),
        "trap_refusal_share": round(sum(r["ok"] for r in traps) / max(1, len(traps)), 3),
        "avg_seconds": round(sum(r["seconds"] for r in rows) / max(1, len(rows)), 1),
        "failed": [r["id"] for r in rows if not r["ok"]],
        "rows": rows,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / f"chat_{args.project}_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, ensure_ascii=False, indent=2))
    print("отчёт:", out)
    return 1 if summary["empty_answers"] or summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
