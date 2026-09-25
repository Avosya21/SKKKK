"""Ящик вопросов внешнего агента из командной строки (для оператора/Claude на сервере, без входа по паролю).

  python scripts/kb_inbox.py list [--project 18] [--status pending]
  python scripts/kb_inbox.py show <id>
  python scripts/kb_inbox.py draft <id>                       # черновик локальной модели (наружу не уходит)
  python scripts/kb_inbox.py answer <id> --author claude --text "..." [--cited req-pilot-0184 ...]
  python scripts/kb_inbox.py approve <id>                     # опубликовать черновик
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.app.config import get_settings  # noqa: E402
from app.app.db import SessionLocal  # noqa: E402
from app.app.services import agent_inbox as inbox  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "show", "draft", "answer", "approve"])
    ap.add_argument("id", nargs="?", type=int)
    ap.add_argument("--project", type=int, default=18)
    ap.add_argument("--status")
    ap.add_argument("--author", default="claude", choices=sorted(inbox.AUTHORS))
    ap.add_argument("--text")
    ap.add_argument("--cited", nargs="*", default=[])
    a = ap.parse_args()
    db = SessionLocal()
    try:
        if a.cmd == "list":
            for r in inbox.list_questions(db, a.project, a.status):
                print(f"#{r.id} req={r.request_id} [{r.status}] {r.question[:90]}")
            return 0
        row = inbox.get_question(db, a.project, a.id)
        if a.cmd == "draft":
            row = inbox.draft_with_llm(db, get_settings(), a.project, a.id)
        elif a.cmd == "answer":
            row = inbox.answer_question(db, a.project, a.id, text=a.text or "", author=a.author, cited=a.cited)
        elif a.cmd == "approve":
            row = inbox.approve_draft(db, a.project, a.id, approved_by="operator")
        print(json.dumps({"id": row.id, "request_id": row.request_id, "status": row.status, "question": row.question,
                          "sources": json.loads(row.sources_json), "draft": row.draft_text, "answer": row.answer_text, "by": row.answer_author},
                         ensure_ascii=False, indent=1))
        return 0
    except ValueError as exc:
        print("ОШИБКА:", exc)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
