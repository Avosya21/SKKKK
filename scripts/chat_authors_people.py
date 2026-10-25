"""Авторы уже загруженных переписок и разобранных реплик → карточки людей проекта (docs/plans/PEOPLE-FLOW-PLAN.md, ЛГ6).

    venv\\Scripts\\python.exe scripts\\chat_authors_people.py                     # план (база не меняется)
    venv\\Scripts\\python.exe scripts\\chat_authors_people.py --skip-project 35   # без этих проектов
    venv\\Scripts\\python.exe scripts\\chat_authors_people.py --apply             # применить; копия БД в data/backups/

То же, что теперь делается при загрузке переписки (``people.chat_author_people``): автор, известный по написанию, — его
человек (ник становится написанием); сотрудник по имени в карточке — его человек; иначе новая карточка с пометкой
«Новая карточка» и компанией по умолчанию (на подтверждение). Слова автора привязываются к карточке. Так же — авторы
уже разобранных реплик (``speech_acts``) без человека, кроме документов («Документ «…»») и неподписанных меток спикеров:
у каждого слова должна быть карточка. Только проекты с включённой платформой. План считается прогоном на временной копии БД; имена — только в консоль (не в git).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def use_db_copy_for_dry_run() -> Path:
    """План считается на временной копии БД: вложенные точки сохранения SQLite (``begin_nested``) без начатой транзакции
    при выходе фиксируют данные, и откат «пробного» прогона их уже не вернул бы (так 16.10 в базу попали три карточки).
    Вызывать до импорта модулей приложения."""
    import os
    import sqlite3
    import tempfile

    src = ROOT / "data" / "app.db"
    dst = Path(tempfile.mkdtemp(prefix="primaryanalitics-dry-")) / "app.db"
    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)
    os.environ["DATABASE_URL"] = "sqlite:///" + dst.as_posix()
    return dst


def run(db, settings, *, skip: set[int]) -> tuple[Counter, list[str]]:
    """Карточки авторов всех переписок (без commit): итог и список новых карточек «пр. N: имя»."""
    from sqlalchemy import select

    from app.app.models.artifact import Artifact
    from app.app.models.person import Person
    from app.app.models.project import Project
    from app.app.models.speech_act import SpeechAct
    from app.app.services import memory_platform, people
    from app.app.services.chat_export import load_artifact_export

    total: Counter = Counter()
    before = set(db.scalars(select(Person.id)).all())
    for project in db.scalars(select(Project).where(Project.is_active.is_(True)).order_by(Project.id)).all():
        if project.id in skip or not memory_platform.enabled(settings, project):
            continue
        for art in db.scalars(select(Artifact).where(Artifact.project_id == project.id, Artifact.source_type == "document")).all():
            export = load_artifact_export(settings, art)
            if not export:
                continue
            total["переписок"] += 1
            total.update(people.chat_author_people(db, project.id, [m.get("author") for m in export.get("messages") or []]))
        orphan = db.scalars(select(SpeechAct.author).where(SpeechAct.project_id == project.id, SpeechAct.person_id.is_(None),
                                                         SpeechAct.author.isnot(None)).distinct()).all()
        orphan = [a for a in orphan if not a.startswith("Документ «")]
        if orphan:
            total["авторов реплик"] += len(orphan)
            total.update(people.chat_author_people(db, project.id, orphan))
    db.flush()
    fresh = [f"пр. {p.project_id}: {p.display_name}" for p in db.scalars(select(Person).where(Person.id.notin_(before))).all()]
    return total, fresh


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="применить (иначе только план)")
    ap.add_argument("--skip-project", type=int, action="append", default=[], help="не трогать проект (можно несколько раз)")
    args = ap.parse_args(argv)
    if not args.apply:
        use_db_copy_for_dry_run()

    import app.app.models  # noqa: F401
    from app.app.config import get_settings
    from app.app.db import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        if args.apply:
            db_path = settings.data_dir / "app.db"
            backup = db_path.parent / "backups" / f"app.db.bak-chat-authors-{datetime.now():%Y%m%d-%H%M%S}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db_path, backup)
            print("копия БД:", backup)
        total, fresh = run(db, settings, skip=set(args.skip_project))
        for line in fresh:
            print("новая карточка —", line)
        print(f"переписок: {total['переписок']}; авторов реплик без человека: {total['авторов реплик']}; авторов известных: {total['known']}, сотрудников: {total['staff']}, "
              f"новых карточек: {total['created']}, пропущено: {total['skipped']}")
        if args.apply:
            db.commit()
            print("готово")
        else:
            db.rollback()
            print("база не менялась; применить — с --apply")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
