"""Показать сырые оценки семантического поиска без отсечки по порогу."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.app.config import get_settings  # noqa: E402
from app.app.db import SessionLocal, get_engine  # noqa: E402
from app.app.services.ollama_client import embed_texts  # noqa: E402
from app.app.services.vector_index import init_vector_store, search_vectors  # noqa: E402


def main() -> int:
    project_id = int(sys.argv[1])
    queries = sys.argv[2:] or ["срок сдачи проекта"]

    settings = get_settings()
    init_vector_store(get_engine())
    db = SessionLocal()
    try:
        for query in queries:
            vec = embed_texts(settings, [query])[0]
            hits = search_vectors(db, vec, project_id=project_id, limit=5)
            print(f"\nЗапрос: {query}")
            if not hits:
                print("  индекс пуст")
            for hit in hits:
                snippet = hit["text"][:70].replace("\n", " ")
                print(f"  score={hit['score']:.3f}  {snippet}")
        print(f"\nтекущий similarity_threshold={settings.similarity_threshold}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
