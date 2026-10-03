"""Страница «Граф зависимостей» (/admin/map/graph): все сущности проекта и все связи между ними. Только admin.

Читает граф из выгрузки `data/project_map_graph.json` (её делает `scripts/export_project_graph.py`);
если выгрузки нет или она старше индекса — собирает граф из самого индекса и обновляет выгрузку.
Боевая БД проекта (data/app.db) не читается: только индекс карты и манифест.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.app.dependencies import get_current_user, require_html_user
from app.app.models.user import User
from app.app.services.project_index import graph as G
from app.app.services.project_index import search as index_search
from app.app.services.project_index import store

router = APIRouter(tags=["admin-map-graph"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

MAX_LIMIT = 1500
MIN_LIMIT = 50
_CACHE: dict[str, Any] = {"graph": None, "stamp": 0.0}


def _admin_only(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")


def load_graph(*, refresh: bool = False) -> dict[str, Any]:
    """Граф проекта: из выгрузки, если она свежее индекса; иначе — сборка из индекса (с кэшем в процессе)."""
    stamp = G.GRAPH_PATH.stat().st_mtime if G.GRAPH_PATH.is_file() else 0.0
    if not refresh and _CACHE["graph"] is not None and _CACHE["stamp"] >= stamp:
        return _CACHE["graph"]
    graph: dict[str, Any] | None = None
    db_stamp = store.DB_PATH.stat().st_mtime if store.DB_PATH.is_file() else 0.0
    if stamp and stamp >= db_stamp:
        try:
            graph = json.loads(G.GRAPH_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            graph = None
    if graph is None:
        conn = store.connect()
        try:
            graph = G.build(conn)
        finally:
            conn.close()
        try:  # обновляем выгрузку, чтобы страница открывалась мгновенно
            G.GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
            G.GRAPH_PATH.write_text(G.to_json(graph), encoding="utf-8", newline="")
            stamp = G.GRAPH_PATH.stat().st_mtime
        except OSError:
            pass
    _CACHE["graph"] = graph
    _CACHE["stamp"] = stamp or time.time()
    return graph


def _stale_counts() -> dict[str, Any]:
    try:
        conn = store.connect()
        try:
            stale = index_search.staleness(conn)
        finally:
            conn.close()
        return {"changed": len(stale["changed"]), "removed": len(stale["removed"]),
                "no_vector": stale["embed_pending"], "age_hours": stale["age_hours"]}
    except Exception:  # noqa: BLE001 — страница карты не должна падать из-за недоступного индекса
        return {"changed": 0, "removed": 0, "no_vector": 0, "age_hours": None}


def _split(value: str) -> set[str]:
    return {part.strip() for part in (value or "").split(",") if part.strip()}


def _relation_matches(relation: str, patterns: set[str]) -> bool:
    """Вид связи подходит под фильтр: точное имя или шаблон с `*` (`m:*` — все смысловые связи манифеста)."""
    if not patterns:
        return True
    return any(relation == pattern or (pattern.endswith("*") and relation.startswith(pattern[:-1]))
               for pattern in patterns)


def _with_ide(nodes: list[dict]) -> list[dict]:
    for node in nodes:
        if node.get("path") and node.get("line"):
            node["ide"] = index_search.ide_link(node["path"], node["line"])
    return nodes


def _trim(view_graph: dict[str, Any], *, kinds: set[str], relations: set[str], limit: int) -> tuple[list[dict], list[dict], bool]:
    """Отфильтровать и обрезать вид по видам узлов и связей (сначала самые связанные узлы)."""
    nodes = [n for n in view_graph["nodes"] if not kinds or n["kind"] in kinds]
    keep = {n["id"] for n in nodes}
    edges = [e for e in view_graph["edges"]
             if e["from"] in keep and e["to"] in keep and _relation_matches(e["rel"], relations)]
    truncated = False
    if len(nodes) > limit:
        degree: dict[str, int] = {}
        for edge in edges:
            degree[edge["from"]] = degree.get(edge["from"], 0) + edge.get("n", 1)
            degree[edge["to"]] = degree.get(edge["to"], 0) + edge.get("n", 1)
        ordered = sorted(nodes, key=lambda n: (-degree.get(n["id"], 0), n["id"]))[:limit]
        keep = {n["id"] for n in ordered}
        nodes = ordered
        edges = [e for e in edges if e["from"] in keep and e["to"] in keep]
        truncated = True
    return nodes, edges, truncated


def _modules_view(graph: dict[str, Any], *, kinds: set[str], relations: set[str], limit: int) -> dict[str, Any]:
    modules = G.module_graph(graph)
    if relations:
        modules["edges"] = [e for e in modules["edges"] if e["rel"] in relations]
    nodes, edges, truncated = _trim(modules, kinds=kinds or {"file"}, relations=relations, limit=limit)
    return {"view": "modules", "center": None, "nodes": nodes, "edges": edges, "truncated": truncated}


@router.get("/admin/map/graph", response_class=HTMLResponse)
def admin_map_graph_page(request: Request, user: Annotated[User, Depends(require_html_user)]):
    _admin_only(user)
    return templates.TemplateResponse(request, "admin_map_graph.html", {
        "title": "Граф зависимостей",
        "node_kinds": G.NODE_LABELS,
        "relation_labels": G.RELATION_LABELS,
        "subsystems": [name for _, name in G.SUBSYSTEMS],
    })


@router.get("/admin/map/graph/summary")
def admin_map_graph_summary(user: Annotated[User, Depends(get_current_user)]):
    """Цифры графа и состояние индекса — для легенды и фильтров страницы."""
    _admin_only(user)
    try:
        graph = load_graph()
    except FileNotFoundError:
        return JSONResponse({"stats": {"nodes": 0, "edges": 0, "by_kind": {}, "by_relation": {}, "by_group": {}},
                             "source": {}, "notes": ["индекс не построен: venv\\Scripts\\python.exe scripts\\index_project_map.py"],
                             "stale": {}})
    return {"stats": graph.get("stats") or G.stats(graph), "source": graph.get("source") or {},
            "built_at": graph.get("built_at"), "notes": graph.get("notes") or [],
            "groups": sorted({n.get("group") for n in graph["nodes"] if n.get("group")}),
            "stale": _stale_counts()}


@router.get("/admin/map/graph/data")
def admin_map_graph_data(
    user: Annotated[User, Depends(get_current_user)],
    focus: Annotated[str, Query(max_length=300)] = "",
    view: Annotated[str, Query(pattern="^(entities|modules)?$")] = "",
    group: Annotated[str, Query(max_length=80)] = "",
    kinds: Annotated[str, Query(max_length=300)] = "",
    relations: Annotated[str, Query(max_length=600)] = "",
    direction: Annotated[str, Query(pattern="^(both|in|out)$")] = "both",
    depth: Annotated[int, Query(ge=1, le=4)] = 1,
    limit: Annotated[int, Query(ge=MIN_LIMIT, le=MAX_LIMIT)] = 600,
):
    """Узлы и рёбра вокруг фокуса, подсистемы или модульный свод — только чтение индекса."""
    _admin_only(user)
    kind_set, relation_set = _split(kinds), _split(relations)
    try:
        graph = load_graph()
    except FileNotFoundError:
        return JSONResponse({"view": "entities", "center": None, "nodes": [], "edges": [], "truncated": False,
                             "total_nodes": 0, "total_edges": 0, "source": {}, "stale": {},
                             "notes": ["индекс не построен: venv\\Scripts\\python.exe scripts\\index_project_map.py"]})
    stats = graph.get("stats") or G.stats(graph)
    payload: dict[str, Any]
    if view == "modules" or (not focus and not group):
        payload = _modules_view(graph, kinds=kind_set, relations=relation_set, limit=limit)
    elif focus:
        result = G.neighbors(graph, focus, depth=depth, direction=direction, kinds=kind_set or None,
                             relations=relation_set or None, limit=limit)
        nodes, edges, truncated = _trim({"nodes": result["nodes"], "edges": result["edges"]},
                                        kinds=set(), relations=set(), limit=limit)
        payload = {"view": "entities", "center": result["center"], "nodes": nodes, "edges": edges,
                   "truncated": result["truncated"] or truncated}
    else:
        subset = {"nodes": [n for n in graph["nodes"] if n.get("group") == group],
                  "edges": graph["edges"]}
        nodes, edges, truncated = _trim(subset, kinds=kind_set, relations=relation_set, limit=limit)
        payload = {"view": "entities", "center": None, "nodes": nodes, "edges": edges, "truncated": truncated}
    payload["nodes"] = _with_ide(payload["nodes"])
    payload["limit"] = limit
    payload["total_nodes"] = stats["nodes"]
    payload["total_edges"] = stats["edges"]
    payload["source"] = graph.get("source") or {}
    payload["stale"] = _stale_counts()
    payload["notes"] = graph.get("notes") or []
    return payload
