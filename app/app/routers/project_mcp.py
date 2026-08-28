from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.app.config import Settings, external_base_url, get_settings
from app.app.db import get_db
from app.app.dependencies import get_current_user, require_role
from app.app.mcp.project_protocol import dispatch_mcp_body
from app.app.models.project import Project
from app.app.models.user import User
from app.app.services.project_mcp_tokens import (
    authenticate_project_mcp_token,
    create_project_mcp_token,
    list_project_mcp_tokens,
    revoke_project_mcp_token,
)

router = APIRouter(tags=["project-mcp"])


class McpTokenCreate(BaseModel):
    label: str = Field(default="Cursor", min_length=1, max_length=128)


def _project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _require_mcp_auth(db: Session, project_id: int, request: Request) -> None:
    _project_or_404(db, project_id)
    token = _bearer_token(request)
    if not authenticate_project_mcp_token(db, project_id=project_id, bearer=token):
        raise HTTPException(status_code=401, detail="Invalid or missing MCP token")


@router.get("/projects/{project_id}/mcp/tokens")
def list_tokens(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    _project_or_404(db, project_id)
    return {"tokens": list_project_mcp_tokens(db, project_id=project_id)}


@router.post("/projects/{project_id}/mcp/tokens", status_code=status.HTTP_201_CREATED)
def create_token(
    project_id: int,
    body: McpTokenCreate,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    _project_or_404(db, project_id)
    row, plain = create_project_mcp_token(db, project_id=project_id, label=body.label, user_id=user.id)
    return {
        "id": row.id,
        "label": row.label,
        "token": plain,
        "token_prefix": row.token_prefix,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.delete("/projects/{project_id}/mcp/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_token(
    project_id: int,
    token_id: int,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    _project_or_404(db, project_id)
    if not revoke_project_mcp_token(db, project_id=project_id, token_id=token_id):
        raise HTTPException(status_code=404, detail="Token not found")
    return None


@router.get("/projects/{project_id}/mcp/info")
def mcp_connection_info(
    project_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    project = _project_or_404(db, project_id)
    base = external_base_url(settings, str(request.base_url))
    mcp_url = f"{base}/mcp/projects/{project_id}"
    return {
        "project_id": project_id,
        "project_name": project.name,
        "mcp_url": mcp_url,
        "public_base_url": base,
        "uses_public_url": bool((settings.web_public_url or "").strip()),
        "transport": "streamable-http",
        "auth_header": "Authorization: Bearer <MCP-токен>",
        "tools_count": 9,
        "read_only": True,
        "cursor_config_example": {
            "mcpServers": {
                f"skkkk-project-{project_id}": {
                    "url": f"{base}/mcp/projects/{project_id}",
                    "headers": {"Authorization": "Bearer <ваш MCP-токен>"},
                }
            }
        },
        "web_port": settings.web_port,
    }


@router.post("/mcp/projects/{project_id}")
@router.get("/mcp/projects/{project_id}")
async def mcp_project_endpoint(
    project_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    _require_mcp_auth(db, project_id, request)

    if request.method == "GET":
        return JSONResponse(
            {
                "name": "skkkk-project",
                "project_id": project_id,
                "transport": "streamable-http",
                "hint": "POST JSON-RPC MCP messages to this URL with Authorization: Bearer token",
            }
        )

    try:
        body: Any = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    responses = dispatch_mcp_body(db, settings, project_id, body)
    if not responses:
        return JSONResponse(status_code=202, content=None)
    if len(responses) == 1:
        return JSONResponse(responses[0])
    return JSONResponse(responses)
