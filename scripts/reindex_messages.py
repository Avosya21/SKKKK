"""Переиндексация переписок по сообщениям (docs/plans/PORTAL-ACCURACY-MANAGER-PLAN.md, Ш2).

Материалы с разметкой сообщений (``*_messages.json``: выгрузка Telegram, копия чата Битрикс24) получают фрагменты по
сообщениям с номерами M… вместо кусков сплошного текста. Модель не нужна, только эмбеддинги ``bge-m3``.

    venv\\Scripts\\python scripts\\reindex_messages.py                 # отчёт: что изменится, ничего не пишет
    venv\\Scripts\\python scripts\\reindex_messages.py --apply         # копия БД, переиндексация, сверка индекса
    venv\\Scripts\\python scripts\\reindex_messages.py --apply --gpu   # то же, эмбеддинги на видеокарте

``--gpu``: только когда видеокарта свободна — очередь пуста и свободно не меньше ``--min-free-mib`` (по умолчанию 2048).
Обычная работа портала считает эмбеддинги на CPU (DECISIONS: Ollama bge-m3 на CPU, чтобы не отбирать видеопамять у LLM
и ASR), поэтому после пересчёта модель выгружается из видеопамяти, и следующий запрос портала загрузит её на CPU.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def free_vram_mib() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20, check=True).stdout
        return int(out.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def busy_queue(db_path: Path) -> int:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute("select count(*) from jobs where status in ('queued','running')").fetchone()[0]
    finally:
        con.close()


def backup(db_path: Path) -> Path:
    target = db_path.parent / "backups" / f"app-before-reindex-messages-{datetime.now():%Y-%m-%d_%H%M%S}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.execute(f"VACUUM INTO '{target.as_posix()}'")
    finally:
        con.close()
    return target


def unload_embedder(base_url: str, model: str) -> None:
    import httpx

    try:
        httpx.post(f"{base_url.rstrip('/')}/api/embed", json={"model": model, "input": [], "keep_alive": 0}, timeout=30)
    except httpx.HTTPError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="переиндексировать (без флага — только отчёт)")
    ap.add_argument("--gpu", action="store_true", help="эмбеддинги на видеокарте; после — выгрузить модель")
    ap.add_argument("--min-free-mib", type=int, default=2048)
    ap.add_argument("--project", type=int, action="append", default=[])
    args = ap.parse_args()

    db_path = ROOT / "data" / "app.db"
    if args.gpu:
        if not args.apply:
            print("--gpu имеет смысл только с --apply")
            return 2
        free = free_vram_mib()
        jobs = busy_queue(db_path)
        if free is None or free < args.min_free_mib or jobs:
            print(f"Видеокарта занята: свободно {free} МиБ (нужно {args.min_free_mib}), задач в очереди {jobs}. Запустите позже или без --gpu.")
            return 3
        os.environ["OLLAMA_NUM_GPU"] = "99"

    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Chunk
    from app.app.services.chat_export import load_artifact_export, owned_elsewhere
    from app.app.services.chunking import chunk_messages
    from app.app.services.indexing import audit_project_index, index_artifact
    from app.app.services.storage import artifact_knowledge_text, parsed_dir

    settings = get_settings()
    report: list[dict] = []
    with SessionLocal() as db:
        q = select(Artifact).where(Artifact.status == "done", Artifact.file_type != "audio")
        if args.project:
            q = q.where(Artifact.project_id.in_(args.project))
        targets = []
        for art in db.scalars(q.order_by(Artifact.project_id, Artifact.id)).all():
            export = load_artifact_export(settings, art)
            if not export or not export.get("messages"):
                continue
            skip = owned_elsewhere(parsed_dir(settings, art.project_id, art.source_type), export)
            new = chunk_messages(export, skip_ids=skip)
            old = db.scalars(select(Chunk).where(Chunk.artifact_id == art.id)).all()
            report.append({"project_id": art.project_id, "artifact_id": art.artifact_id, "messages": len(export["messages"]),
                           "owned_elsewhere": len(skip), "chunks_before": len(old), "chunks_after": len(new),
                           "with_units_before": sum(1 for c in old if c.unit_first)})
            targets.append(art)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"Материалов с перепиской: {len(targets)}; фрагментов было {sum(r['chunks_before'] for r in report)}, "
              f"станет {sum(r['chunks_after'] for r in report)}")
        if not args.apply:
            print("Только отчёт. Применить: --apply (копия БД делается сама).")
            return 0

        copy = backup(db_path)
        print(f"Копия БД: {copy}")
        started = time.monotonic()
        try:
            for art in targets:
                n = index_artifact(db, settings, art, artifact_knowledge_text(settings, art) or art.transcription_text or "")
                db.commit()
                print(f"  {art.artifact_id}: фрагментов {n}")
        finally:
            if args.gpu:
                unload_embedder(settings.ollama_base_url, settings.ollama_embed_model)
                print("bge-m3 выгружена из видеопамяти: портал снова считает эмбеддинги на CPU.")
        print(f"Готово за {time.monotonic() - started:.1f} с ({'GPU' if args.gpu else 'CPU'})")
        for pid in sorted({a.project_id for a in targets}):
            audit = audit_project_index(db, settings, pid)
            bad = [i for i in audit.get("items", []) if i["chunks"] != i["vectors"]]
            print(f"  проект {pid}: сверка индекса — {'расхождений нет' if not bad else bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
