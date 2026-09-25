"""Письма базы знаний внешнему агенту: доставка через MCP, без переноса человеком.

Письма лежат в docs/agent-exchange/letters/NNN_kb_to_<кому>.md (наши исходящие). Агент читает их сам:
`list_artifacts(prefix="contract/letters/")` и `get_artifact_content(artifact_id="contract/letters/<имя>")`; `exchange_status` показывает число и последнее письмо.
Безопасность: отдаются только файлы по строгому шаблону имени из этой папки; путь из запроса не строится (имя сверяется со списком);
письмо видно только ключам того проекта, который указан в его шапке («от: агент базы знаний проекта #N»). Только чтение.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from app.app.config import ROOT

LETTERS_DIR = ROOT / "docs" / "agent-exchange" / "letters"
NAME_RE = re.compile(r"^\d{3}_kb_to_[a-z_]{1,40}\.md$")
PROJECT_RE = re.compile(r"от:\s*агент базы знаний проекта\s*#(\d+)")
PREFIX = "contract/letters/"
MAX_BYTES = 100_000


def list_letters(project_id: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not LETTERS_DIR.is_dir():
        return out
    for path in sorted(LETTERS_DIR.iterdir()):
        if not path.is_file() or not NAME_RE.match(path.name) or path.stat().st_size > MAX_BYTES:
            continue
        text = path.read_text(encoding="utf-8")
        m = PROJECT_RE.search(text[:800])
        if not m or int(m.group(1)) != project_id:
            continue
        title = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("# ")), path.stem)
        out.append({"name": path.name, "artifact_id": PREFIX + path.name, "title": title, "content_hash": hashlib.sha1(text.encode("utf-8")).hexdigest(),
                    "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"), "size_bytes": len(text.encode("utf-8"))})
    return out


def get_letter(project_id: int, artifact_id: str) -> dict[str, Any]:
    name = artifact_id.removeprefix(PREFIX)
    for row in list_letters(project_id):  # имя сверяется со списком, путь из запроса не используется
        if row["name"] == name:
            return {"artifact_id": row["artifact_id"], "type": "letter", "kind": "text", "title": row["title"], "content_hash": row["content_hash"],
                    "content": (LETTERS_DIR / row["name"]).read_text(encoding="utf-8")}
    raise ValueError("Letter not found")


def summary(project_id: int) -> dict[str, Any]:
    rows = list_letters(project_id)
    return {"count": len(rows), "latest": rows[-1]["name"] if rows else None, "latest_hash": rows[-1]["content_hash"] if rows else None}
