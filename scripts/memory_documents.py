"""Документы в память решений (docs/plans/DOCUMENT-MEMORY-PLAN.md): дочитать уже загруженные документы проекта.

По умолчанию — только отчёт: какие документы проекта ещё без актов, сколько в них единиц и порций модели, оценка времени.
С ``--enqueue`` — копия data/app.db в data/backups/ и задачи очереди: ``memory`` на каждый такой документ и одна ``memory_judge``
на проект (итог считается один раз, после всех документов). Созвоны и переписка заново не читаются.

    venv\\Scripts\\python.exe scripts\\memory_documents.py --project 18
    venv\\Scripts\\python.exe scripts\\memory_documents.py --project 18 --enqueue
    venv\\Scripts\\python.exe scripts\\memory_documents.py --all --enqueue
    venv\\Scripts\\python.exe scripts\\memory_documents.py --project 40 --reassign   # заново разложить пункты документов

``--reassign`` (после смены промпта раскладки): копия базы, акты документов снимаются с предметов
(``subject_windows.release_document_acts``; решения человека и реплики остаются), в очередь — итог проекта: он разложит их заново.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECONDS_PER_WINDOW = 9  # замер 2026-10-09: созвон 81 реплика, 11 порций, 100 с


def backup_db(db_path: Path) -> Path:
    dst = db_path.parent / "backups" / f"app_before_doc_memory_{datetime.now():%Y%m%d_%H%M%S}.db"
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    with target:
        source.backup(target)
    source.close()
    target.close()
    return dst


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Дочитать документы проекта в память решений")
    scope = ap.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project", type=int, action="append", help="id проекта (можно несколько раз)")
    scope.add_argument("--all", action="store_true", help="все проекты, где память решений не выключена")
    ap.add_argument("--redo", action="store_true", help="и документы, у которых акты уже есть (например, после смены промпта)")
    ap.add_argument("--artifact", type=int, action="append", help="только эти документы (id материала, можно несколько раз)")
    ap.add_argument("--enqueue", action="store_true", help="сделать копию базы и поставить задачи в очередь воркера")
    ap.add_argument("--reassign", action="store_true", help="заново разложить уже размеченные пункты документов по предметам")
    args = ap.parse_args()
    if args.reassign:
        return reassign(args)

    from sqlalchemy import func, select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.models.speech_act import SpeechAct
    from app.app.services import doc_memory, memory_mode
    from app.app.services.auto_review import MODE_LIVE, MODE_SHADOW, review_mode
    from app.app.services.memory_pipeline import SCOPE_ACTS, SCOPE_JUDGE
    from app.app.services.queue import enqueue_auto_review
    from app.app.services.speech_acts import dialog_units, windows

    settings = get_settings()
    db = SessionLocal()
    projects = db.scalars(select(Project).where(Project.is_active.is_(True)).order_by(Project.id)).all()
    if args.project:
        projects = [p for p in projects if p.id in set(args.project)]
    plan: list[tuple[Project, list[tuple[Artifact, int, int]]]] = []
    for project in projects:
        if memory_mode.mode_of(settings, project) == "off":
            if args.project:
                print(f"проект {project.id} «{project.name}»: память решений выключена — пропуск")
            continue
        docs = []
        for art in db.scalars(select(Artifact).where(Artifact.project_id == project.id, Artifact.status == "done").order_by(Artifact.id)).all():
            if args.artifact and art.id not in set(args.artifact):
                continue
            if not doc_memory.is_document(art) or dialog_units(settings, art):
                continue
            has_acts = db.scalar(select(func.count()).select_from(SpeechAct).where(SpeechAct.artifact_id == art.id)) or 0
            if has_acts and not args.redo:
                continue
            units = doc_memory.document_units(doc_memory.document_text(settings, art), doc_memory.document_label(art))
            if units:
                docs.append((art, len(units), len(windows(units))))
        if docs:
            plan.append((project, docs))

    total = 0
    for project, docs in plan:
        print(f"\nпроект {project.id} «{project.name}»")
        for art, n_units, n_windows in docs:
            total += n_windows
            print(f"  #{art.id:<5} единиц {n_units:>5}  порций {n_windows:>4}  ~{n_windows * SECONDS_PER_WINDOW // 60 + 1} мин  {art.original_filename}")
    print(f"\nвсего порций {total}, ~{total * SECONDS_PER_WINDOW // 60 + 1} мин работы модели (без раскладки по предметам)")
    if not args.enqueue or not plan:
        db.close()
        return 0
    print(f"копия базы: {backup_db(ROOT / 'data' / 'app.db')}")
    for project, docs in plan:
        mode = review_mode(settings, project)
        mode = mode if mode in (MODE_SHADOW, MODE_LIVE) else MODE_SHADOW
        for art, _n, _w in docs:
            job = enqueue_auto_review(db, art.id, scope=SCOPE_ACTS, mode=mode)
            print(f"  в очереди: память #{art.id} {art.original_filename}" + ("" if job else " (уже стоит)"))
        job = enqueue_auto_review(db, docs[-1][0].id, scope=SCOPE_JUDGE, mode=mode)
        print(f"  в очереди: итог проекта {project.id}" + ("" if job else " (уже стоит)"))
    db.close()
    return 0


def reassign(args) -> int:
    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.project import Project
    from app.app.models.speech_act import SpeechAct
    from app.app.services import memory_mode
    from app.app.services.auto_review import MODE_LIVE, MODE_SHADOW, review_mode
    from app.app.services.memory_pipeline import SCOPE_JUDGE, building
    from app.app.services.queue import enqueue_auto_review
    from app.app.services.subject_windows import release_document_acts

    settings = get_settings()
    db = SessionLocal()
    projects = db.scalars(select(Project).where(Project.is_active.is_(True)).order_by(Project.id)).all()
    if args.project:
        projects = [p for p in projects if p.id in set(args.project)]
    projects = [p for p in projects if memory_mode.mode_of(settings, p) != "off"]
    if any(building(db, p.id) for p in projects):
        print("память одного из проектов сейчас строится — дождитесь окончания")
        return 1
    print(f"копия базы: {backup_db(ROOT / 'data' / 'app.db')}")
    for project in projects:
        doc_art = db.scalar(select(SpeechAct.artifact_id).where(SpeechAct.project_id == project.id, SpeechAct.unit_ref.like("P%"))
                            .order_by(SpeechAct.artifact_id.desc()).limit(1))
        if doc_art is None:
            continue
        stats = release_document_acts(db, project.id)
        mode = review_mode(settings, project)
        mode = mode if mode in (MODE_SHADOW, MODE_LIVE) else MODE_SHADOW
        job = enqueue_auto_review(db, doc_art, scope=SCOPE_JUDGE, mode=mode)
        print(f"проект {project.id} «{project.name}»: снято актов {stats['released']}, удалено предметов {stats['dropped_subjects']}, "
              f"с решением человека не тронуто {stats['kept_human']}; итог в очереди: {'да' if job else 'уже стоит'}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
