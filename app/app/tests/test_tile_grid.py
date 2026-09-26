"""Сетка плиток комнаты (пункт 3.11): bestGrid подбирает колонки по ширине и высоте сцены."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

LAYOUT = Path(__file__).resolve().parents[1] / "static" / "meeting_layout.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")


def _grid(count, w, h, gap=12):
    code = (
        f"global.window=undefined;require({json.dumps(str(LAYOUT))});"
        f"console.log(JSON.stringify(globalThis.SKKKKLayout.bestGrid({count},{w},{h},{gap},16/9)))"
    )
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.parametrize("count", [2, 3, 4, 6, 9, 12])
@pytest.mark.parametrize("size", [(1920, 900), (1280, 560), (1024, 500), (800, 420)])
def test_grid_always_fits_stage(count, size):
    w, h = size
    g = _grid(count, w, h)
    assert g["cols"] * g["rows"] >= count
    assert g["cols"] * g["tileW"] + 12 * (g["cols"] - 1) <= w + 1
    assert g["rows"] * g["tileH"] + 12 * (g["rows"] - 1) <= h + 1
    assert abs(g["tileW"] / g["tileH"] - 16 / 9) < 0.06


def test_wide_short_stage_prefers_more_columns():
    wide = _grid(6, 1920, 500)
    tall = _grid(6, 600, 1000)
    assert wide["cols"] > tall["cols"]


def test_single_and_pair():
    assert _grid(1, 1200, 700)["cols"] == 1
    assert _grid(2, 1600, 700)["cols"] == 2
