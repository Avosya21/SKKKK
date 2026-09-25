"""Переобработка загруженных документов новым извлечением таблиц (docs/plans/DOCUMENT-TABLES-PLAN.md, шаг 8).

По умолчанию — только отчёт, ничего не меняет: для каждого документа новое извлечение (_raw) сравнивается с тем,
что сейчас лежит в базе знаний (_clean). С ``--enqueue`` — копия data/app.db в data/backups/ и задачи
``process_artifact`` в обычную очередь: воркер заново извлечёт текст, нормализует, пересоберёт сущности, индекс,
граф и ТЗ. Созвоны, картинки, файлы обмена с агентами и документы с задачей в очереди не трогаются.

    venv\\Scripts\\python.exe scripts\\reprocess_documents.py --project 18
    venv\\Scripts\\python.exe scripts\\reprocess_documents.py --all --types docx,xlsx,pdf --enqueue
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_TYPES = "docx,xlsx,pdf,pptx,html,txt"
TABLE_CHUNK_RE = re.compile(r"^(?:\| --- |Строка \d+: )", re.M)


def backup_db(db_path: Path) -> Path:
    dst = db_path.parent / "backups" / f"app_before_doc_tables_{datetime.now():%Y%m%d_%H%M%S}.db"
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    with target:
        source.backup(target)
    source.close()
    target.close()
    return dst


def extract_raw(artifact, settings) -> str:
    """То же извлечение, что в process_document_artifact, без записи файлов и статусов."""
    from app.app.services import pipeline_documents as pd

    path = Path(artifact.file_path)
    if artifact.file_type == "pdf":
        return pd._extract_pdf(path)
    if artifact.file_type == "docx":
        return pd._extract_docx(path)
    if artifact.file_type == "xlsx":
        from app.app.services.xlsx_extract import extract_spreadsheet

        return extract_spreadsheet(path)
    if artifact.file_type == "pptx":
        from app.app.services.pptx_extract import extract_pptx

        return extract_pptx(path, None)  # без OCR картинок: для отчёта достаточно
    if artifact.file_type == "html":
        return pd._extract_html(path)
    return pd._extract_plain(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Переобработка документов с новым извлечением таблиц")
    scope = ap.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project", type=int, action="append", help="id проекта (можно несколько раз)")
    scope.add_argument("--all", action="store_true", help="все активные проекты")
    ap.add_argument("--types", default=DEFAULT_TYPES, help=f"типы файлов через запятую (по умолчанию {DEFAULT_TYPES})")
    ap.add_argument("--artifact", action="append", help="только эти artifact_id (можно несколько раз)")
    ap.add_argument("--enqueue", action="store_true", help="сделать копию базы и поставить задачи в очередь воркера")
    args = ap.parse_args()

    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Chunk
    from app.app.models.job import Job
    from app.app.models.project import Project
    from app.app.services.agent_gateway import is_exchange_filename
    from app.app.services.queue import enqueue_process_artifact
    from app.app.services.summarize_document import load_document_source_text
    from app.app.services.table_md import split_blocks, table_numbers

    settings = get_settings()
    types = {t.strip() for t in args.types.split(",") if t.strip()}
    db = SessionLocal()
    try:
        q = select(Artifact).where(Artifact.file_type.in_(types), Artifact.status.in_(("done", "error")))
        if args.artifact:
            q = q.where(Artifact.artifact_id.in_(args.artifact))
        if args.project:
            q = q.where(Artifact.project_id.in_(args.project))
        else:
            active = select(Project.id).where(Project.is_active.is_(True))
            q = q.where(Artifact.project_id.in_(active))
        artifacts = [a for a in db.scalars(q.order_by(Artifact.project_id, Artifact.id)).all()
                     if not is_exchange_filename(a.original_filename or "")]
        # Мешает только ждущая обработка того же файла; фоновые автопроверки не в счёт.
        busy = set(db.scalars(select(Job.artifact_id).where(
            Job.status.in_(("queued", "running")), Job.kind == "process_artifact")).all())

        print(f"{'проект':>6} {'artifact_id':<30} {'тип':<5} {'raw_new':>8} {'clean':>8} {'clean/raw':>9} "
              f"{'табл.':>5} {'нет_чисел':>9} {'фрагм.':>6} {'табл.фр':>7}  файл")
        todo: list[Artifact] = []
        for art in artifacts:
            if not Path(art.file_path or "").is_file():
                print(f"{art.project_id:>6} {art.artifact_id:<30} {art.file_type:<5} — исходный файл не найден: {art.file_path}")
                continue
            try:
                raw = extract_raw(art, settings)
            except Exception as exc:  # noqa: BLE001 — отчёт по остальным важнее
                print(f"{art.project_id:>6} {art.artifact_id:<30} {art.file_type:<5} — ошибка извлечения: {exc}")
                continue
            clean = load_document_source_text(settings, art) or ""
            clean_digits = re.sub(r"[  ]", "", clean)
            missing = [n for n in dict.fromkeys(table_numbers(raw)) if n not in clean_digits]
            tables = sum(1 for b in split_blocks(raw) if b.kind == "table")
            chunks = list(db.scalars(select(Chunk.text).where(Chunk.artifact_id == art.id)).all())
            table_chunks = sum(1 for t in chunks if TABLE_CHUNK_RE.search(t or ""))
            dense = lambda t: len(re.sub(r"\s+", "", t))  # noqa: E731 — без пробелов: отступы HTML не считаются потерей
            ratio = dense(clean) / dense(raw) if raw.strip() else 0.0
            print(f"{art.project_id:>6} {art.artifact_id:<30} {art.file_type:<5} {len(raw):>8} {len(clean):>8} "
                  f"{ratio:>9.2f} {tables:>5} {len(missing):>9} {len(chunks):>6} {table_chunks:>7}  "
                  f"{art.original_filename}")
            if art.id in busy:
                print("       └ уже есть задача в очереди — пропуск")
                continue
            todo.append(art)

        if not args.enqueue:
            print(f"\nОтчёт без изменений. Документов к переобработке: {len(todo)}. Запуск: добавьте --enqueue.")
            return 0
        if not todo:
            print("\nНечего ставить в очередь.")
            return 0
        db_path = Path(settings.database_url.split("sqlite:///", 1)[-1])
        if settings.database_url.startswith("sqlite:///") and db_path.is_file():
            print(f"\nКопия базы: {backup_db(db_path)}")
        for art in todo:
            art.error_message = None
            job = enqueue_process_artifact(db, art.id, phase="document")
            print(f"  в очереди job {job.id}: {art.artifact_id} {art.original_filename}")
        print(f"Поставлено задач: {len(todo)}. Воркер обработает их по очереди (статусы — на странице проекта).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
