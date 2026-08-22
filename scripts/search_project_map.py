"""Semantic search over SKKKK project map index.

  py scripts/search_project_map.py "как работает merge"
  py scripts/search_project_map.py "speaker recalculate" --limit 8
"""

from __future__ import annotations

import argparse
import sqlite3
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.app.config import Settings
from app.app.services.ollama_client import EMBED_DIM, embed_texts

DB_PATH = ROOT / "data" / "project_map.db"


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def search(query: str, *, limit: int) -> list[dict]:
    if not DB_PATH.is_file():
        raise SystemExit(f"missing {DB_PATH} — run: py scripts/index_project_map.py")

    settings = Settings()
    vec = embed_texts(settings, [query])[0]
    q = struct.pack(f"{len(vec)}f", *vec)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _load_sqlite_vec(conn)
    rows = conn.execute(
        f"""
        SELECT c.source_path, c.kind, c.title, c.text,
               vec_distance_cosine(v.embedding, ?) AS distance
        FROM map_vectors v
        JOIN map_chunks c ON c.id = v.chunk_id
        ORDER BY distance ASC
        LIMIT ?
        """,
        (q, limit),
    ).fetchall()
    conn.close()

    out = []
    for row in rows:
        out.append(
            {
                "score": round(1.0 - float(row["distance"]), 3),
                "source": row["source_path"],
                "kind": row["kind"],
                "title": row["title"],
                "text": row["text"][:1200],
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="Search query in Russian or English")
    parser.add_argument("--limit", type=int, default=6)
    args = parser.parse_args()

    hits = search(args.query, limit=args.limit)
    if not hits:
        print("no results")
        return 0

    for i, hit in enumerate(hits, 1):
        print(f"\n--- [{i}] score={hit['score']} {hit['kind']}:{hit['title']} ({hit['source']}) ---")
        print(hit["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
