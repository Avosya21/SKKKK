"""Склонение по числу (пункт 3.2): проверка JS-хелпера через node."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")


def _plural(n: int) -> str:
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("  plural(n, forms) {")
    end = src.index("  count(n, forms)")
    body = "const H = {" + src[start:end].rstrip().rstrip(",") + "};"
    code = body + f"console.log(H.plural({n}, ['срок','срока','сроков']))"
    return subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()


@pytest.mark.parametrize(
    "n,expected",
    [(0, "сроков"), (1, "срок"), (2, "срока"), (4, "срока"), (5, "сроков"), (11, "сроков"), (12, "сроков"),
     (14, "сроков"), (21, "срок"), (22, "срока"), (25, "сроков"), (101, "срок"), (111, "сроков")],
)
def test_ru_plural(n, expected):
    assert _plural(n) == expected
