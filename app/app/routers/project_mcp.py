"""Ключи проектного MCP, сам MCP-эндпоинт и обмен с внешним агентом: настройки, вердикты, ящик вопросов, журнал."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.app.config import Settings, external_base_url, get_settings
from app.app.db import get_db
from app.app.dependencies import get_current_user, require_role
from app.app.services.project_access import require_project
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
    can_submit: bool = False  # устаревшее: true = «вопросы и вердикты»; лучше передавать scope
    scope: str | None = Field(default=None, max_length=24)  # read | questions | questions_verdicts
    agent_name: str | None = Field(default=None, max_length=64)
    rate_limit: int | None = Field(default=None, ge=1, le=1000)  # обращений за 10 минут; пусто — общий лимит


class McpScopeUpdate(BaseModel):
    scope: str = Field(max_length=24)
    agent_name: str | None = Field(default=None, max_length=64)
    rate_limit: int | None = Field(default=None, ge=1, le=1000)
    clear_rate_limit: bool = False


def _project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if not project or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _owned_project(db: Session, user: User, project_id: int) -> Project:
    return require_project(db, user, project_id)


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _require_mcp_auth(db: Session, project_id: int, request: Request):
    _project_or_404(db, project_id)
    token = _bearer_token(request)
    row = authenticate_project_mcp_token(db, project_id=project_id, bearer=token)
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or missing MCP token")
    return row


@router.get("/projects/{project_id}/agent-exchange")
def agent_exchange_log(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
    limit: int = 100,
    decision: str | None = None,
    source: str | None = None,
    kind: str | None = None,
):
    """Журнал обмена с внешним агентом (что и когда он присылал, что решил шлюз); фильтры: decision, source (например mcp), kind."""
    import json

    from sqlalchemy import select

    from app.app.models.agent_exchange import AgentExchangeLog

    _owned_project(db, user, project_id)
    query = select(AgentExchangeLog).where(AgentExchangeLog.project_id == project_id)
    if decision:
        query = query.where(AgentExchangeLog.decision == decision)
    if source:
        query = query.where(AgentExchangeLog.source.startswith(source))
    if kind:
        query = query.where(AgentExchangeLog.kind == kind)
    rows = db.scalars(query.order_by(AgentExchangeLog.id.desc()).limit(max(1, min(limit, 500)))).all()
    from app.app.models.project_mcp_token import ProjectMcpToken

    token_ids = {int(r.source[4:]) for r in rows if r.source.startswith("mcp:") and r.source[4:].isdigit()}
    names = {t.id: (t.agent_name or t.label) for t in db.scalars(select(ProjectMcpToken).where(ProjectMcpToken.id.in_(token_ids))).all()} if token_ids else {}
    return {
        "items": [
            {"id": r.id, "at": r.at.isoformat() if r.at else None, "source": r.source,
             "agent": names.get(int(r.source[4:])) if r.source.startswith("mcp:") and r.source[4:].isdigit() else None, "kind": r.kind, "request_id": r.request_id,
             "decision": r.decision, "risk": r.risk, "reasons": json.loads(r.reasons_json or "[]"), "size_bytes": r.size_bytes, "artifact_id": r.artifact_id}
            for r in rows
        ]
    }


def _question_dict(r, full: bool = True) -> dict:
    import json

    d = {"id": r.id, "request_id": r.request_id, "status": r.status, "created_at": r.created_at.isoformat() if r.created_at else None,
         "question": r.question, "requirement_ids": json.loads(r.requirement_ids_json or "[]"), "answer_author": r.answer_author}
    if full:
        d.update({"sources": json.loads(r.sources_json or "[]"), "draft": r.draft_text, "draft_author": r.draft_author, "answer": r.answer_text})
    return d


@router.get("/projects/{project_id}/agent-settings")
def get_agent_settings(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    """Настройки обмена с агентами: участники и стороны, пояс, пакет якорей, автоответ (читать могут admin и manager проекта)."""
    from app.app.services import agent_settings

    project = _owned_project(db, user, project_id)
    return {"settings": agent_settings.get_settings_for(project), "packs": agent_settings.available_packs(), "can_edit": user.role == "admin"}


@router.put("/projects/{project_id}/agent-settings")
def put_agent_settings(
    project_id: int, body: dict,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin"))],
):
    """Изменить настройки обмена с агентами (только admin). Контракт пересобирается при следующем обращении агента."""
    from app.app.services import agent_settings
    from app.app.services import contract as ctr

    project = _owned_project(db, user, project_id)
    try:
        saved = agent_settings.save(project, body)
    except agent_settings.SettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    ctr._cache.pop(project_id, None)
    return {"settings": saved}


@router.get("/projects/{project_id}/agent-exchange/summary")
def agent_exchange_summary(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    """Счётчики для заголовков карточек: вопросы ждут ответа, вердикты ждут подтверждения, отклонено за сутки, требований без подтверждения."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from app.app.models.agent_exchange import AgentExchangeLog
    from app.app.services import agent_inbox as inbox
    from app.app.services import agent_verdicts as av
    from app.app.services import requirement_review as rr

    _owned_project(db, user, project_id)
    qs = inbox.list_questions(db, project_id, limit=500)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    rejected = db.scalar(select(func.count()).select_from(AgentExchangeLog).where(
        AgentExchangeLog.project_id == project_id, AgentExchangeLog.decision != "accepted", AgentExchangeLog.at >= since))
    return {"questions_waiting": sum(1 for q in qs if q.status in ("pending", "drafted")), "verdicts_waiting": av.list_verdicts(db, project_id, pending_only=True)["waiting"],
            "rejected_24h": rejected or 0,
            # «ждут человека»: не проверено никем, модель не уверена / в тени, или попало в контрольную выборку
            "requirements_unconfirmed": rr.review_list(db, get_settings(), project_id, verification="needs_human", limit=1)["total"]}


@router.get("/projects/{project_id}/agent-verdicts")
def agent_verdicts_list(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
    pending_only: bool = False,
):
    from app.app.services import agent_verdicts as av

    _owned_project(db, user, project_id)
    return {**av.list_verdicts(db, project_id, pending_only=pending_only), "can_edit": user.role == "admin"}


@router.post("/projects/{project_id}/agent-verdicts/{entity_id}/{action}")
def agent_verdict_action(
    project_id: int, entity_id: int, action: str, body: dict,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin"))],
):
    from app.app.services import agent_verdicts as av

    _owned_project(db, user, project_id)
    try:
        return av.apply(db, project_id, entity_id, action, user=user, reason=body.get("reason"))
    except av.VerdictError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/projects/{project_id}/agent-inbox")
def agent_inbox_list(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
    status: str | None = None,
):
    """Ящик вопросов внешнего агента (pending → drafted → answered)."""
    from app.app.services import agent_inbox as inbox

    _owned_project(db, user, project_id)
    return {"items": [_question_dict(r) for r in inbox.list_questions(db, project_id, status)]}


@router.post("/projects/{project_id}/agent-inbox/{question_id}/draft")
def agent_inbox_draft(
    project_id: int, question_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin"))],
):
    """Черновик ответа от локальной модели (наружу не публикуется до одобрения)."""
    from app.app.config import get_settings
    from app.app.services import agent_inbox as inbox

    _owned_project(db, user, project_id)
    try:
        return _question_dict(inbox.draft_with_llm(db, get_settings(), project_id, question_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/projects/{project_id}/agent-inbox/{question_id}/answer")
def agent_inbox_answer(
    project_id: int, question_id: int, body: dict,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin"))],
):
    """Опубликовать ответ. Тело: {"text": "...", "cited": ["req-..."]}; либо {"approve_draft": true}."""
    from app.app.services import agent_inbox as inbox

    _owned_project(db, user, project_id)
    try:
        if body.get("approve_draft"):
            row = inbox.approve_draft(db, project_id, question_id, approved_by=user.name)
        else:
            row = inbox.answer_question(db, project_id, question_id, text=str(body.get("text", "")), author="human", cited=body.get("cited") or [])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _question_dict(row)


@router.post("/projects/{project_id}/mcp/tokens/{token_id}/submit")
def set_token_submit(
    project_id: int, token_id: int, body: dict,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    """Разрешить/запретить ключу класть вопросы и вердикты в очередь обмена (submit_question, submit_verdicts). По умолчанию выключено."""
    from app.app.services.project_mcp_tokens import set_project_mcp_token_submit

    _owned_project(db, user, project_id)
    if not set_project_mcp_token_submit(db, project_id=project_id, token_id=token_id, enabled=bool(body.get("enabled"))):
        raise HTTPException(status_code=404, detail="Token not found")
    return {"ok": True, "can_submit": bool(body.get("enabled"))}


@router.post("/projects/{project_id}/mcp/tokens/{token_id}/scope")
def set_token_scope(
    project_id: int, token_id: int, body: McpScopeUpdate,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    """Сменить права ключа (read / questions / questions_verdicts), имя агента и лимит обращений за 10 минут."""
    from app.app.services.project_mcp_tokens import set_project_mcp_token_scope

    _owned_project(db, user, project_id)
    try:
        ok = set_project_mcp_token_scope(db, project_id=project_id, token_id=token_id, scope=body.scope, agent_name=body.agent_name,
                                         rate_limit=body.rate_limit, update_limit=body.clear_rate_limit or body.rate_limit is not None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"ok": True, "scope": body.scope}


@router.get("/projects/{project_id}/mcp/tokens")
def list_tokens(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    _owned_project(db, user, project_id)
    return {"tokens": list_project_mcp_tokens(db, project_id=project_id)}


@router.post("/projects/{project_id}/mcp/tokens", status_code=status.HTTP_201_CREATED)
def create_token(
    project_id: int,
    body: McpTokenCreate,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    _owned_project(db, user, project_id)
    try:
        row, plain = create_project_mcp_token(db, project_id=project_id, label=body.label, user_id=user.id, can_submit=body.can_submit,
                                              scope=body.scope, agent_name=body.agent_name, rate_limit=body.rate_limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from app.app.services.project_mcp_tokens import token_scope

    return {
        "can_submit": token_scope(row) != "read",
        "scope": token_scope(row),
        "agent_name": row.agent_name,
        "rate_limit": row.rate_limit,
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
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    _owned_project(db, user, project_id)
    if not revoke_project_mcp_token(db, project_id=project_id, token_id=token_id):
        raise HTTPException(status_code=404, detail="Token not found")
    return None


@router.get("/projects/{project_id}/mcp/info")
def mcp_connection_info(
    project_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_role("admin", "manager"))],
):
    project = _owned_project(db, user, project_id)
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
    # БД и инструменты синхронные (wait_reply ждёт до минуты): всё блокирующее уходит в пул потоков, иначе замирает весь сервер
    token_row = await run_in_threadpool(_require_mcp_auth, db, project_id, request)

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

    responses = await run_in_threadpool(dispatch_mcp_body, db, settings, project_id, body, token=token_row)
    if not responses:
        return JSONResponse(status_code=202, content=None)
    if len(responses) == 1:
        return JSONResponse(responses[0])
    return JSONResponse(responses)
