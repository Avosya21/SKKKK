"""scripts/audit_integrity.py: индекс поиска сверяется с нарезкой текущего текста материала (docs/FINDINGS-2026-09.md, п. 67)."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

from app.app.services.chunking import chunk_document, chunk_transcript

ROOT = Path(__file__).resolve().parents[3]


def _load_audit():
    spec = importlib.util.spec_from_file_location("audit_integrity", ROOT / "scripts" / "audit_integrity.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_integrity"] = mod
    spec.loader.exec_module(mod)
    return mod


def _db(tmp_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(tmp_path / "app.db")
    con.execute(
        "CREATE TABLE artifacts (id INTEGER PRIMARY KEY, artifact_id TEXT, project_id INTEGER, file_type TEXT, "
        "source_type TEXT, status TEXT, transcription_text TEXT, transcription_file_path TEXT)"
    )
    con.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, artifact_id INTEGER, chunk_index INTEGER, text TEXT)")
    return con


def _index(con: sqlite3.Connection, pk: int, texts: list[str]) -> None:
    con.executemany("INSERT INTO chunks (artifact_id, chunk_index, text) VALUES (?, ?, ?)", [(pk, i, t) for i, t in enumerate(texts)])


def test_stale_index_flags_transcript_rewritten_after_indexing(tmp_path):
    audit = _load_audit()
    con = _db(tmp_path)
    calls = tmp_path / "projects" / "7" / "parsed" / "calls"
    calls.mkdir(parents=True)
    final = calls / "art_call_final.txt"
    final.write_text("[00:01 - 00:05] Менеджер: Добрый день.\n[00:05 - 00:09] Аналитик: Начнём с оплаты.\n", encoding="utf-8")
    doc = "## Раздел\nТекст документа."
    con.executemany(
        "INSERT INTO artifacts VALUES (?, ?, 7, ?, ?, 'done', ?, NULL)",
        [
            (1, "art_call", "audio", "call", None),
            (2, "art_doc", "txt", "document", doc),
            (3, "art_agent", "txt", "document", "{}"),  # файл обмена с агентом: готов без индекса
        ],
    )
    _index(con, 1, [c.text for c in chunk_transcript(final.read_text(encoding="utf-8").strip())])
    _index(con, 2, [c.text for c in chunk_document(doc)])
    assert audit.stale_index(con, tmp_path) == []

    # имя спикера поменяли после индексации, а переиндексации не было
    final.write_text("[00:01 - 00:05] Менеджер проекта: Добрый день.\n[00:05 - 00:09] Аналитик: Начнём с оплаты.\n", encoding="utf-8")
    assert audit.stale_index(con, tmp_path) == [(1, "art_call")]
