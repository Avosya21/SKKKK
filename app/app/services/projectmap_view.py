"""Мост портала к инструменту projectmap (`tools/projectmap`): обзорная карта проекта для страницы `/admin/map/overview`.

Граф строит сам инструмент (`projectmap index`, файл `.projectmap/out/graph.json`, в git не хранится); отсюда он только читается и
превращается в страницу просмотрщика. Раскладку считать долго (секунды), поэтому страница кэшируется по времени файла графа;
пересборка графа — под замком, чтобы два клика не запускали два разбора проекта.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = ROOT / "tools" / "projectmap"

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"stamp": None, "html": ""}


def _tool():
    """Модули projectmap: каталог инструмента добавляется в путь импорта один раз."""
    if str(TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(TOOL_DIR))
    from projectmap import cli, config, export, view

    return cli, config, export, view


def graph_path() -> Path:
    _, config, _, _ = _tool()
    return ROOT / config.load_config(ROOT).graph


def state() -> dict[str, Any]:
    """Есть ли граф и когда он построен (время файла)."""
    path = graph_path()
    if not path.is_file():
        return {"exists": False, "built_at": None}
    return {"exists": True, "built_at": path.stat().st_mtime}


def rebuild() -> dict[str, Any]:
    """Построить граф заново (разбор проекта, метрики, история git) и сбросить кэш страницы."""
    cli, config, _, _ = _tool()
    with _LOCK:
        cfg = config.load_config(ROOT)
        graph = cli.build_and_write(ROOT, cfg, ROOT / cfg.graph)
        _CACHE["stamp"] = None
    stats = graph.get("stats") or {}
    return {"built_at": graph_path().stat().st_mtime, "nodes": stats.get("nodes"), "edges": stats.get("edges"), "notes": graph.get("notes") or []}


def page_html() -> str:
    """Страница просмотрщика по текущему графу; графа нет — строится при первом обращении."""
    cli, config, export, view = _tool()
    with _LOCK:
        cfg = config.load_config(ROOT)
        path = ROOT / cfg.graph
        if not path.is_file():
            cli.build_and_write(ROOT, cfg, path)
        stamp = path.stat().st_mtime
        if _CACHE["stamp"] == stamp and _CACHE["html"]:
            return _CACHE["html"]
        graph = json.loads(path.read_text(encoding="utf-8"))
        html = export.render_html(view.build_view(graph, title=cfg.title or ROOT.name, ide=cfg.ide, root=ROOT.as_posix()))
        _CACHE["stamp"], _CACHE["html"] = stamp, html
        return html
