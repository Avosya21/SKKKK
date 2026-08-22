"""Build vector index of SKKKK project map for semantic search (NotebookLM-style).

Indexes:
  - docs/PROJECT-MAP.md (by ## sections)
  - docs/project-map/manifest.yaml (nodes + edges as text)
  - Key source modules (module docstring + public defs)
  - docs/prompts/*.md (first 2k chars each)

Store: data/project_map.db (sqlite-vec, separate from app.db)

  py scripts/index_project_map.py
  py scripts/index_project_map.py --full   # also scan all app/services and asr/*.py
"""

from __future__ import annotations

import argparse
import ast
import re
import sqlite3
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from app.app.config import Settings
from app.app.services.ollama_client import EMBED_DIM, embed_texts

DB_PATH = ROOT / "data" / "project_map.db"
MAP_MD = ROOT / "docs" / "PROJECT-MAP.md"
MANIFEST = ROOT / "docs" / "project-map" / "manifest.yaml"

CHUNK_SOURCES = [
    ROOT / "app" / "app" / "main.py",
    ROOT / "app" / "app" / "workers" / "gpu_worker.py",
    ROOT / "asr" / "run_pipeline.py",
    ROOT / "asr" / "merge_ensemble.py",
    ROOT / "asr" / "build_transcript_view.py",
]

FULL_SCAN_DIRS = [
    ROOT / "app" / "app" / "services",
    ROOT / "app" / "app" / "routers",
    ROOT / "asr",
]


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS map_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            kind TEXT NOT NULL,
            title TEXT,
            text TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS map_vectors USING vec0(
            chunk_id INTEGER PRIMARY KEY,
            embedding float[{EMBED_DIM}]
        )
        """
    )


def _split_markdown_sections(path: Path) -> list[tuple[str, str]]:
    body = path.read_text(encoding="utf-8")
    parts = re.split(r"(?=^## \d+\.)", body, flags=re.MULTILINE)
    out: list[tuple[str, str]] = []
    for part in parts:
        part = part.strip()
        if len(part) < 80:
            continue
        first = part.split("\n", 1)[0].strip("# ").strip()
        out.append((first, part))
    return out


def _manifest_chunks(path: Path) -> list[tuple[str, str, str]]:
    if not path.is_file():
        return []
    raw = path.read_text(encoding="utf-8")
    if yaml is None:
        return [("manifest", "manifest.yaml", raw[:8000])]
    data = yaml.safe_load(raw)
    chunks: list[tuple[str, str, str]] = []
    for node in data.get("nodes") or []:
        text = (
            f"NODE {node.get('id')}\n"
            f"kind={node.get('kind')} path={node.get('path')}\n"
            f"title={node.get('title')}\n"
            f"tags={node.get('tags')}\n"
        )
        chunks.append((node.get("id", "?"), "manifest_node", text))
    for edge in data.get("edges") or []:
        text = (
            f"EDGE {edge.get('from')} --[{edge.get('relation')}]--> {edge.get('to')}\n"
        )
        chunks.append((f"{edge.get('from')}->{edge.get('to')}", "manifest_edge", text))
    return chunks


def _module_summary(path: Path) -> str | None:
    if not path.is_file() or path.name.startswith("test_"):
        return None
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (SyntaxError, OSError):
        return None
    doc = ast.get_docstring(tree) or ""
    defs: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.append(node.name)
    head = "\n".join(defs[:40])
    body = f"FILE {path.relative_to(ROOT)}\n{doc}\n\nSymbols: {', '.join(defs[:40])}"
    if len(body) > 6000:
        body = body[:6000] + "\n…"
    return body if doc or defs else None


def _prompt_chunks() -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for path in sorted((ROOT / "docs" / "prompts").glob("*.md")):
        text = path.read_text(encoding="utf-8")[:4000]
        out.append((path.stem, "prompt", f"PROMPT {path.name}\n{text}"))
    return out


def collect_chunks(*, full: bool) -> list[dict]:
    rows: list[dict] = []
    idx = 0

    if MAP_MD.is_file():
        for title, text in _split_markdown_sections(MAP_MD):
            rows.append(
                {
                    "source_path": str(MAP_MD.relative_to(ROOT)),
                    "chunk_index": idx,
                    "kind": "map_section",
                    "title": title,
                    "text": text,
                }
            )
            idx += 1

    for nid, kind, text in _manifest_chunks(MANIFEST):
        rows.append(
            {
                "source_path": str(MANIFEST.relative_to(ROOT)),
                "chunk_index": idx,
                "kind": kind,
                "title": nid,
                "text": text,
            }
        )
        idx += 1

    for title, kind, text in _prompt_chunks():
        rows.append(
            {
                "source_path": "docs/prompts",
                "chunk_index": idx,
                "kind": kind,
                "title": title,
                "text": text,
            }
        )
        idx += 1

    paths = list(CHUNK_SOURCES)
    if full:
        for d in FULL_SCAN_DIRS:
            paths.extend(sorted(d.glob("*.py")))

    seen: set[str] = set()
    for path in paths:
        rel = str(path.relative_to(ROOT))
        if rel in seen:
            continue
        seen.add(rel)
        summary = _module_summary(path)
        if not summary:
            continue
        rows.append(
            {
                "source_path": rel,
                "chunk_index": idx,
                "kind": "source_module",
                "title": path.name,
                "text": summary,
            }
        )
        idx += 1

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Index SKKKK project map vectors")
    parser.add_argument("--full", action="store_true", help="Index all services/routers/asr modules")
    args = parser.parse_args()

    rows = collect_chunks(full=args.full)
    if not rows:
        print("no chunks to index", file=sys.stderr)
        return 1

    settings = Settings()
    texts = [r["text"] for r in rows]
    print(f"embedding {len(texts)} chunks via Ollama {settings.ollama_embed_model}…", flush=True)
    vectors = embed_texts(settings, texts)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    _load_sqlite_vec(conn)
    _init_db(conn)
    conn.execute("DELETE FROM map_vectors")
    conn.execute("DELETE FROM map_chunks")

    for row, vec in zip(rows, vectors, strict=True):
        cur = conn.execute(
            """
            INSERT INTO map_chunks(source_path, chunk_index, kind, title, text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (row["source_path"], row["chunk_index"], row["kind"], row["title"], row["text"]),
        )
        cid = cur.lastrowid
        conn.execute(
            "INSERT INTO map_vectors(chunk_id, embedding) VALUES (?, ?)",
            (cid, struct.pack(f"{len(vec)}f", *vec)),
        )

    conn.commit()
    conn.close()
    print(f"wrote {DB_PATH} chunks={len(rows)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
