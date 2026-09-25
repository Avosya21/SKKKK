"""Страница «Карта проекта» для администратора: поиск по индексу кода, связей, БД, окружения и истории (/admin/map)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.app.dependencies import get_current_user, require_html_user
from app.app.models.user import User
from app.app.services.project_index import search as index_search
from app.app.services.project_index import store

router = APIRouter(tags=["admin-map"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _admin_only(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/admin/map", response_class=HTMLResponse)
def admin_map_page(request: Request, user: Annotated[User, Depends(require_html_user)]):
    _admin_only(user)
    return templates.TemplateResponse(request, "admin_map.html", {"title": "Карта проекта", "modes": index_search.MODES})


@router.get("/admin/map/search")
def admin_map_search(
    user: Annotated[User, Depends(get_current_user)],
    q: Annotated[str, Query(max_length=300)] = "",
    mode: Annotated[str, Query(max_length=24)] = "search",
    limit: Annotated[int, Query(ge=1, le=30)] = 8,
):
    """Только чтение индекса (data/project_map.db); боевая БД не затрагивается."""
    _admin_only(user)
    if mode not in index_search.MODES:
        raise HTTPException(status_code=422, detail="Неизвестный режим поиска")
    try:
        conn = store.connect()
    except FileNotFoundError:
        return JSONResponse({"mode": mode, "query": q, "items": [], "notes": ["индекс не построен: scripts/index_project_map.py"]})
    try:
        result = index_search.unified(conn, mode, q.strip(), limit)
        stale = index_search.staleness(conn)
    finally:
        conn.close()
    for it in result["items"]:
        it["ide"] = index_search.ide_link(it["path"], it["line"]) if it["path"] else ""
    result["stale"] = {"changed": len(stale["changed"]), "removed": len(stale["removed"]), "no_vector": stale["embed_pending"],
                       "age_hours": stale["age_hours"]}
    return result
