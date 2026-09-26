"""Пересобрать выжимки созвонов, индекс, сущности и ТЗ по проекту(ам).

  venv\\Scripts\\python.exe scripts\\refresh_portal_knowledge.py --project 19
  venv\\Scripts\\python.exe scripts\\refresh_portal_knowledge.py --all
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.app.config import get_settings
from app.app.db import SessionLocal
from app.app.models.artifact import Artifact
from app.app.models.project import Project
from app.app.services.entities import reextract_project_entities
from app.app.services.indexing import index_artifact
from app.app.services.project_tasks import (
    TASK_ARTIFACT_REINDEX,
    TASK_CALL_SUMMARY,
    TASK_ENTITIES_REEXTRACT,
    TASK_TZ_REBUILD,
    TaskAlreadyRunning,
    list_active_tasks,
    track_project_task,
)
from app.app.services.speaker_profiles import (
    rebuild_artifact_call_summary,
    refresh_artifact_project_index,
)
from app.app.services.tz import rebuild_tz


def _wait_idle(db, project_id: int, timeout_sec: int = 3600) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        active = list_active_tasks(db, project_id=project_id)
        blocking = [
            t
            for t in active
            if t["task_type"]
            in {TASK_CALL_SUMMARY, TASK_ARTIFACT_REINDEX, TASK_ENTITIES_REEXTRACT, TASK_TZ_REBUILD}
        ]
        if not blocking:
            return
        labels = ", ".join(f"{t['task_type']}#{t['id']}" for t in blocking)
        print(f"  ждём активные задачи: {labels}", flush=True)
        time.sleep(8)
        db.rollback()
    raise TimeoutError(f"проект {project_id}: задачи не завершились за {timeout_sec}с")


def _refresh_project(project_id: int, *, skip_summaries: bool) -> int:
    settings = get_settings()
    db = SessionLocal()
    errors = 0
    try:
        project = db.get(Project, project_id)
        if project is None or not project.is_active:
            print(f"проект {project_id}: не найден")
            return 1
        print(f"\n=== {project.id} «{project.name}» ===", flush=True)
        _wait_idle(db, project_id)

        artifacts = list(
            db.scalars(select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.id)).all()
        )
        audios = [a for a in artifacts if a.file_type == "audio" and a.status == "done"]
        docs = [a for a in artifacts if a.file_type != "audio" and a.status == "done"]

        if not skip_summaries:
            for art in audios:
                print(f"выжимка {art.artifact_id} ({art.original_filename})…", flush=True)
                try:
                    with track_project_task(
                        db,
                        task_type=TASK_CALL_SUMMARY,
                        project_id=project_id,
                        artifact_db_id=art.id,
                    ):
                        rebuild_artifact_call_summary(
                            db, project_id=project_id, artifact_id=art.artifact_id
                        )
                    print(f"  ok выжимка {art.artifact_id}", flush=True)
                except TaskAlreadyRunning as exc:
                    print(f"  skip (занято): {exc}", flush=True)
                    _wait_idle(db, project_id)
                except Exception as exc:
                    errors += 1
                    print(f"  ERROR выжимка {art.artifact_id}: {exc}", flush=True)

        for art in audios:
            print(f"индекс {art.artifact_id}…", flush=True)
            try:
                with track_project_task(
                    db,
                    task_type=TASK_ARTIFACT_REINDEX,
                    project_id=project_id,
                    artifact_db_id=art.id,
                ):
                    result = refresh_artifact_project_index(
                        db, project_id=project_id, artifact_id=art.artifact_id
                    )
                print(f"  ok chunks={result.get('chunks')}", flush=True)
            except Exception as exc:
                errors += 1
                print(f"  ERROR индекс {art.artifact_id}: {exc}", flush=True)

        for art in docs:
            text = (art.transcription_text or "").strip()
            if not text:
                print(f"индекс {art.artifact_id}: нет текста, пропуск")
                continue
            print(f"индекс документ {art.artifact_id}…", flush=True)
            try:
                n = index_artifact(db, settings, art, text)
                db.commit()
                print(f"  ok chunks={n}", flush=True)
            except Exception as exc:
                errors += 1
                db.rollback()
                print(f"  ERROR индекс {art.artifact_id}: {exc}", flush=True)

        print("сущности и сроки…", flush=True)
        try:
            with track_project_task(
                db,
                task_type=TASK_ENTITIES_REEXTRACT,
                project_id=project_id,
            ):
                ent = reextract_project_entities(db, settings, project_id)
            print(
                f"  ok {ent['succeeded']}/{ent['artifacts']} "
                f"deadlines={ent['tasks_with_deadline']}",
                flush=True,
            )
        except Exception as exc:
            errors += 1
            print(f"  ERROR сущности: {exc}", flush=True)

        print("ТЗ проекта…", flush=True)
        try:
            with track_project_task(
                db,
                task_type=TASK_TZ_REBUILD,
                project_id=project_id,
            ):
                path = rebuild_tz(db, settings, project_id, manual=True)
            print(f"  ok {path}", flush=True)
        except Exception as exc:
            errors += 1
            print(f"  ERROR ТЗ: {exc}", flush=True)
        return 1 if errors else 0
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh portal knowledge data")
    parser.add_argument("--project", type=int, action="append", help="Project id (repeatable)")
    parser.add_argument("--all", action="store_true", help="All active projects")
    parser.add_argument(
        "--skip-summaries",
        action="store_true",
        help="Do not rebuild call_summary.md (wait/use existing)",
    )
    args = parser.parse_args()
    db = SessionLocal()
    try:
        if args.all:
            ids = list(db.scalars(select(Project.id).where(Project.is_active.is_(True)).order_by(Project.id)))
        elif args.project:
            ids = args.project
        else:
            parser.error("Specify --project ID or --all")
    finally:
        db.close()

    rc = 0
    for pid in ids:
        rc = max(rc, _refresh_project(pid, skip_summaries=args.skip_summaries))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
