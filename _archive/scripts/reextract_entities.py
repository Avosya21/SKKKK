"""Re-run entity extraction (tasks, deadlines) for processed artifacts."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.app.config import get_settings
from app.app.db import SessionLocal
from app.app.models.artifact import Artifact
from app.app.services.entities import reextract_artifact_entities, reextract_project_entities


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-extract entities (tasks/deadlines) via LLM")
    parser.add_argument("--project", type=int, help="Project id — all done artifacts")
    parser.add_argument("--artifact", help="Single artifact_id")
    args = parser.parse_args()
    if bool(args.project) == bool(args.artifact):
        parser.error("Specify exactly one of --project or --artifact")

    settings = get_settings()
    db = SessionLocal()
    try:
        if args.project:
            result = reextract_project_entities(db, settings, args.project)
            print(
                f"project {args.project}: {result['succeeded']}/{result['artifacts']} ok, "
                f"tasks_with_deadline={result['tasks_with_deadline']}"
            )
            for row in result["results"]:
                if row.get("ok"):
                    print(
                        f"  {row['artifact_id']}: tasks={row['tasks']}, "
                        f"with_deadline={row['tasks_with_deadline']}"
                    )
                else:
                    print(f"  {row.get('artifact_id')}: ERROR {row.get('error')}")
            return 0 if result["succeeded"] == result["artifacts"] else 1

        artifact = db.scalar(select(Artifact).where(Artifact.artifact_id == args.artifact))
        if not artifact:
            raise SystemExit(f"artifact not found: {args.artifact}")
        result = reextract_artifact_entities(db, settings, artifact)
        print(
            f"{result['artifact_id']}: tasks={result['tasks']}, "
            f"with_deadline={result['tasks_with_deadline']}"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
