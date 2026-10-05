"""Совместимость: разбор Python в ядре projectmap даёт те же символы и вызовы, что прежний разбор индекса портала.

Сверка идёт на настоящих файлах репозитория. Импортов ядро находит больше (учитывает импорты внутри функций), поэтому для них
проверяется только «не меньше».
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.app.services.project_index import chunkers
from projectmap.lang_python import extract_python

ROOT = Path(__file__).resolve().parents[3]
FILES = sorted(str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "app" / "app" / "services").glob("*.py"))[:60] + \
        ["app/app/main.py", "app/app/routers/knowledge.py", "app/app/models/entity.py", "scripts/check_map_drift.py"]


def _kind(kind: str) -> str:
    return kind if kind in ("module", "class", "constant") else "fn"


@pytest.mark.parametrize("path", FILES)
def test_core_extraction_matches_the_portal_index(path):
    text = (ROOT / path).read_text(encoding="utf-8-sig")
    old = chunkers.chunk_python(path, text, 120)
    core = extract_python(path, text)
    assert {(s.qualname, _kind(s.kind)) for s in core.symbols} == {(s.qualname, _kind(s.kind)) for s in old.symbols}
    assert {(c.caller, c.callee, c.qualifier) for c in core.calls} == {(c.caller, c.callee, c.qualifier or "") for c in old.calls}
    assert {i.module for i in core.imports} >= {i.module for i in old.imports}
