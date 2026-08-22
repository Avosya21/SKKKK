from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.services import project_mcp_data as data

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "skkkk-project"
SERVER_VERSION = "1.0.0"

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "project_overview",
        "description": "Краткая сводка по проекту: имя, число артефактов, сущностей, chunks.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_artifacts",
        "description": "Список загруженных материалов проекта (файлы, статусы, ссылки на отчёты).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Фильтр по статусу, например done"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_project_summary",
        "description": "Итоговая выжимка (ТЗ) по всему проекту — current_tz.md.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "search_project",
        "description": "Семантический поиск по проиндексированным фрагментам проекта (RAG).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 12},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_entities",
        "description": "Извлечённые сущности (решения, задачи, факты) по материалам проекта.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_type": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_knowledge_graph",
        "description": "Узлы и связи графа знаний проекта.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_notes",
        "description": "Пользовательские заметки по проекту.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_conflicts",
        "description": "Потенциальные конфликты между сущностями и их статус.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_artifact_content",
        "description": "Текст материала: выжимка созвона, стенограмма или entities.json.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "minLength": 1},
                "kind": {
                    "type": "string",
                    "enum": ["auto", "summary", "transcript", "entities"],
                    "default": "auto",
                },
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
    },
]


def _tool_result(payload: Any) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def call_tool(
    db: Session,
    settings: Settings,
    project_id: int,
    name: str,
    arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    args = arguments or {}
    try:
        if name == "project_overview":
            return _tool_result(data.mcp_project_overview(db, settings, project_id))
        if name == "list_artifacts":
            return _tool_result(
                data.mcp_list_artifacts(
                    db,
                    project_id,
                    status=args.get("status"),
                    limit=int(args.get("limit") or 100),
                )
            )
        if name == "get_project_summary":
            return _tool_result(data.mcp_get_project_summary(db, settings, project_id))
        if name == "search_project":
            query = str(args.get("query") or "").strip()
            if not query:
                return _tool_error("query is required")
            return _tool_result(
                data.mcp_search_project(
                    db,
                    settings,
                    project_id,
                    query=query,
                    limit=int(args.get("limit") or 12),
                )
            )
        if name == "list_entities":
            return _tool_result(
                data.mcp_list_entities(
                    db,
                    project_id,
                    entity_type=args.get("entity_type"),
                    limit=int(args.get("limit") or 100),
                )
            )
        if name == "get_knowledge_graph":
            return _tool_result(
                data.mcp_get_knowledge_graph(db, project_id, limit=int(args.get("limit") or 200))
            )
        if name == "list_notes":
            return _tool_result(data.mcp_list_notes(db, project_id, limit=int(args.get("limit") or 50)))
        if name == "list_conflicts":
            return _tool_result(data.mcp_list_conflicts(db, project_id, limit=int(args.get("limit") or 50)))
        if name == "get_artifact_content":
            artifact_id = str(args.get("artifact_id") or "").strip()
            if not artifact_id:
                return _tool_error("artifact_id is required")
            return _tool_result(
                data.mcp_get_artifact_content(
                    db,
                    settings,
                    project_id,
                    artifact_id=artifact_id,
                    kind=str(args.get("kind") or "auto"),
                )
            )
        return _tool_error(f"Unknown tool: {name}")
    except ValueError as exc:
        return _tool_error(str(exc))
    except Exception as exc:
        return _tool_error(f"{type(exc).__name__}: {exc}")


def handle_jsonrpc(
    db: Session,
    settings: Settings,
    project_id: int,
    message: dict[str, Any],
) -> dict[str, Any] | None:
    """Обработать одно JSON-RPC сообщение MCP. Notifications → None."""
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    f"Read-only MCP доступ к знаниям проекта #{project_id} Primary Analytics. "
                    "Используйте search_project и get_project_summary для контекста."
                ),
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        tool_name = params.get("name")
        result = call_tool(db, settings, project_id, str(tool_name), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if msg_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def dispatch_mcp_body(
    db: Session,
    settings: Settings,
    project_id: int,
    body: Any,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]]
    if isinstance(body, list):
        messages = body
    elif isinstance(body, dict):
        messages = [body]
    else:
        return [{"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}]

    responses: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            responses.append({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid message"}})
            continue
        resp = handle_jsonrpc(db, settings, project_id, message)
        if resp is not None:
            responses.append(resp)
    return responses
