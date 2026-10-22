"""Шаг 1. Копия базы портала и выгрузка материала для опыта.

Боевую базу только читаем: копия делается через SQLite backup API (учитывает WAL,
не блокирует запись портала). Дальше опыт работает только с копией.

    experiments\\embeddinggemma2\\.venv\\Scripts\\python.exe experiments\\embeddinggemma2\\snapshot.py
"""
from __future__ import annotations

import glob
import json
import sqlite3
from pathlib import Path

import sqlite_vec

from common import LIVE_DB, OUT, ROOT, SNAPSHOT_DB, write_jsonl


def copy_db() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(SNAPSHOT_DB)
    src.backup(dst)
    dst.close()
    src.close()
    print(f"[snapshot] {SNAPSHOT_DB} ({SNAPSHOT_DB.stat().st_size / 1e6:.0f} MB)")


def export_chunks(con: sqlite3.Connection) -> None:
    rows = con.execute(
        "SELECT id, project_id, artifact_id, chunk_index, text, speaker, start_sec, end_sec FROM chunks ORDER BY id"
    ).fetchall()
    cols = ["id", "project_id", "artifact_id", "chunk_index", "text", "speaker", "start_sec", "end_sec"]
    write_jsonl(OUT / "chunks.jsonl", [dict(zip(cols, r)) for r in rows])
    print(f"[snapshot] chunks: {len(rows)}")


def export_bge_vectors(con: sqlite3.Connection) -> None:
    """Векторы BGE-M3, которые сейчас в портале, — базовая линия для сравнения."""
    import numpy as np

    con.enable_load_extension(True)
    sqlite_vec.load(con)
    rows = con.execute("SELECT chunk_id, embedding FROM chunk_vectors ORDER BY chunk_id").fetchall()
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    vecs = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    np.save(OUT / "bge_ids.npy", ids)
    np.save(OUT / "bge_vecs.npy", vecs)
    print(f"[snapshot] bge vectors: {vecs.shape}")


def export_frames() -> None:
    """Кадры демонстрации экрана: путь к jpg + описание, которое сейчас пишет vision-модель."""
    out = []
    for fj in glob.glob(str(ROOT / "data" / "projects" / "*" / "artifacts" / "*" / "lab" / "runs" / "*" / "frames.json")):
        p = Path(fj)
        project_id = int(p.parts[p.parts.index("projects") + 1])
        artifact_dir = p.parents[3].name
        for i, fr in enumerate(json.loads(p.read_text(encoding="utf-8"))):
            jpg = p.parent / fr["path"]
            if not jpg.is_file():
                continue
            out.append({
                "frame_key": f"{artifact_dir}:{p.parent.name}:{fr.get('frame_id', i)}",
                "project_id": project_id,
                "artifact_dir": artifact_dir,
                "t_rel": fr.get("t_rel"),
                "jpg": str(jpg),
                "text": fr.get("text") or "",
                "text_source": fr.get("text_source"),
            })
    write_jsonl(OUT / "frames.jsonl", out)
    print(f"[snapshot] frames: {len(out)}")


def main() -> None:
    copy_db()
    con = sqlite3.connect(f"file:{SNAPSHOT_DB}?mode=ro", uri=True)
    export_chunks(con)
    export_bge_vectors(con)
    con.close()
    export_frames()


if __name__ == "__main__":
    main()
