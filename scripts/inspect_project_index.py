"""Сверка векторного индекса проекта: чанки ↔ sqlite-vec ↔ lab _final.txt."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.app.config import get_settings
from app.app.db import SessionLocal
from app.app.services.indexing import audit_project_index


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить sqlite-vec индекса проекта")
    parser.add_argument("project_id", nargs="?", type=int, help="id проекта")
    parser.add_argument("--project", type=int, default=None, help="id проекта")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    project_id = args.project or args.project_id or 18

    settings = get_settings()
    db = SessionLocal()
    try:
        report = audit_project_index(db, settings, project_id)
    finally:
        db.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    print(f"проект {report['project_id']} «{report['project_name']}»")
    print(f"материалов {report['artifacts']}, чанков {report['chunks']}, векторов {report['vectors']}")
    print("статус:", "ок" if report["ok"] else "есть расхождения")
    for item in report["items"]:
        spk = ", ".join(item["speakers"][:8]) or "—"
        flag = ""
        if item["chunks"] != item["vectors"]:
            flag = "  ! векторы"
        elif item["chunks"] and not item["aligned_with_final"]:
            flag = "  ! не _final.txt"
        print(
            f"  {item['status']:16} {item['artifact_id']}  "
            f"chunks={item['chunks']} vec={item['vectors']}  "
            f"speakers={spk}{flag}"
        )
        print(f"                   {item['filename']}")
    for problem in report["problems"]:
        print("problem:", problem)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
