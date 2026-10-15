"""Переиндексация созвонов с показом экрана без дублей «Речь на экране» (2026-10-15).

«Речь на экране» — та же речь, нарезанная по отрезку кадра и перемешанная по словам. Теперь она не попадает в индекс
(``chunking.chunk_transcript``); уже проиндексированные созвоны пересчитываются этим скриптом. Модель не нужна, только
эмбеддинги ``bge-m3``.

    venv\\Scripts\\python scripts\\reindex_screen_calls.py                  # отчёт
    venv\\Scripts\\python scripts\\reindex_screen_calls.py --apply --gpu    # копия БД, пересчёт на видеокарте, выгрузка модели
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from reindex_messages import backup, busy_queue, free_vram_mib, unload_embedder  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--min-free-mib", type=int, default=2048)
    args = ap.parse_args()
    db_path = ROOT / "data" / "app.db"
    if args.gpu:
        free, jobs = free_vram_mib(), busy_queue(db_path)
        if not args.apply or free is None or free < args.min_free_mib or jobs:
            print(f"Видеокарта занята или нет --apply: свободно {free} МиБ, задач в очереди {jobs}.")
            return 3
        os.environ["OLLAMA_NUM_GPU"] = "99"

    from sqlalchemy import func, select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Chunk
    from app.app.services.chunking import chunk_transcript
    from app.app.services.indexing import index_artifact
    from app.app.services.storage import artifact_knowledge_text

    settings = get_settings()
    with SessionLocal() as db:
        targets = []
        for art in db.scalars(select(Artifact).where(Artifact.status == "done", Artifact.file_type == "audio").order_by(Artifact.id)).all():
            text = artifact_knowledge_text(settings, art) or ""
            if "Речь на экране:" not in text:
                continue
            before = db.scalar(select(func.count()).select_from(Chunk).where(Chunk.artifact_id == art.id)) or 0
            after = len(chunk_transcript(text))
            targets.append((art, text))
            print(f"проект {art.project_id} материал {art.id}: фрагментов {before} → {after}")
        print(f"Созвонов с «Речью на экране»: {len(targets)}")
        if not args.apply:
            print("Только отчёт. Применить: --apply [--gpu].")
            return 0
        print("Копия БД:", backup(db_path))
        started = time.monotonic()
        try:
            for art, text in targets:
                index_artifact(db, settings, art, text)
                db.commit()
        finally:
            if args.gpu:
                unload_embedder(settings.ollama_base_url, settings.ollama_embed_model)
                print("bge-m3 выгружена из видеопамяти: портал снова считает эмбеддинги на CPU.")
        print(f"Готово за {time.monotonic() - started:.1f} с")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
