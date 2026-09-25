"""Проектный MCP по JSON-RPC: инструменты чтения знаний проекта и приёма данных агента, разбор запросов и инструкции клиенту."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.services import project_mcp_contract as contract_tools
from app.app.services import project_mcp_data as data

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "skkkk-project"
SERVER_VERSION = "1.0.0"

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_guide",
        "description": "ВЫЗОВИ ПЕРВЫМ. Руководство для агента: как читать базу, формат данных, как задавать вопросы и просить нас о чём-то, что мы делаем и не делаем.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "project_overview",
        "description": "Краткая сводка по проекту: имя, число артефактов, сущностей, chunks.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_artifacts",
        "description": "Список материалов проекта: тип, дата загрузки и дата события, статус; плюс файлы контракта contract/*.json (префикс — параметр prefix).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Фильтр по статусу, например done"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "prefix": {"type": "string", "description": "Префикс имени, например contract/"},
                "updated_since": {"type": "string", "description": "ISO-8601: только материалы, загруженные после этого момента (файлы контракта сравнивай по content_hash)"},
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
                "cursor": {"type": "string", "description": "Для contract/*.json: курсор следующей порции"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_requirements",
        "description": "Контракт: требования с id, дословной цитатой, автором, стороной, датой, статусом, in_current_tz, code_anchors. Порциями (cursor).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["proposed", "agreed", "agreed_out_of_scope", "rejected", "implemented", "unknown"]},
                "updated_since": {"type": "string", "description": "ISO-8601: только добавленные/изменённые после этого момента"},
                "requirement_ids": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "cursor": {"type": "string"},
                "fields": {"type": "string", "enum": ["full", "brief"], "description": "brief — без цитат и длинных текстов (~500 байт на требование); цитату бери через get_quote"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_question",
        "description": "ЗАПИСЬ (нужны права ключа «вопросы» или «вопросы и вердикты»): положить свободный вопрос в очередь базы знаний. Отвечает человек, Claude или локальная модель (с одобрением); ответ читай через wait_reply. Только добавление. Повтор с тем же client_request_id вернёт первый результат.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "integer", "description": "Необязателен: если не передан, номер выдаст сервер и вернёт в ответе"},
                "question": {"type": "string", "maxLength": 2000},
                "requirement_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                "client_request_id": {"type": "string", "maxLength": 64, "description": "Ключ идемпотентности для повторов при обрыве связи"},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_verdicts",
        "description": "ЗАПИСЬ (нужны права ключа «вопросы и вердикты»): передать вердикты по требованиям. Пишутся как analysis_verdict с пометкой untrusted_source и ждут подтверждения человеком. Только добавление; прежний вердикт по требованию гасится мягко, ничего не удаляется.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "integer", "description": "Необязателен: если не передан, номер выдаст сервер"},
                "items": {"type": "array", "maxItems": 500, "items": {"type": "object"}, "description": "Как в analysis_reply_<n>.json: requirement_id, verdict, confidence, code_evidence, data_evidence, blockers, questions_for_customer, anchor_corrections"},
                "client_request_id": {"type": "string", "maxLength": 64},
            },
            "required": ["items"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait_reply",
        "description": "Дождаться ответа на обращение (analysis_request/question/reply) одним вызовом вместо опроса: вернёт тело contract/kb_reply_<request_id>.json, когда статус перестанет быть pending (или по истечении wait_s, до 60).",
        "inputSchema": {
            "type": "object",
            "properties": {"request_id": {"type": ["string", "integer"]}, "wait_s": {"type": "integer", "minimum": 0, "maximum": 60, "default": 30}},
            "required": ["request_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "exchange_status",
        "description": "Состояние обмена: сколько вопросов ждут ответа, когда был последний ответ, сколько вердиктов ждёт подтверждения человеком.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_changes",
        "description": "Дельта контракта после момента since: какие требования/решения/расхождения добавились, изменились, исчезли.",
        "inputSchema": {
            "type": "object",
            "properties": {"since": {"type": "string", "description": "ISO-8601"}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_code_anchors",
        "description": "Только точки входа в код (1С/Битрикс24) по требованиям — гипотезы, которые агент анализа проверяет.",
        "inputSchema": {
            "type": "object",
            "properties": {"requirement_ids": {"type": "array", "items": {"type": "string"}}, "system": {"type": "string", "enum": ["1c", "b24"]}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_conflicts",
        "description": "Расхождения контракта с двумя цитатами (сейчас правило scope: согласовано, но вне текущего ТЗ).",
        "inputSchema": {
            "type": "object",
            "properties": {"min_severity": {"type": "string", "enum": ["low", "medium", "high"]}, "status": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_quote",
        "description": "Дословные цитаты с автором, стороной и временем для требования/решения (по id или id сущности).",
        "inputSchema": {
            "type": "object",
            "properties": {"requirement_id": {"type": "string"}, "entity_id": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_artifact_meta",
        "description": "Тип, дата события, участники со сторонами, внешние ссылки материала.",
        "inputSchema": {"type": "object", "properties": {"artifact_id": {"type": "string", "minLength": 1}}, "required": ["artifact_id"], "additionalProperties": False},
    },
    {
        "name": "get_tz",
        "description": "Текущее ТЗ по пунктам с привязкой к требованиям (requirement_ids).",
        "inputSchema": {"type": "object", "properties": {"version": {"type": "string"}}, "additionalProperties": False},
    },
]


def _tool_result(payload: Any, *, compact: bool = False) -> dict[str, Any]:
    # compact — для больших ответов контракта (лимит ответа 200 КБ считается по этому виду)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if compact else json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_error(message: str, *, tool: str = "", detail: str = "") -> dict[str, Any]:
    """Ошибка — JSON {"error","tool","detail"}, а не стектрейс (требование контракта обмена)."""
    body = {"error": message, "tool": tool, "detail": detail}
    return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": True}


def call_tool(
    db: Session,
    settings: Settings,
    project_id: int,
    name: str,
    arguments: dict[str, Any] | None,
    token: Any = None,
) -> dict[str, Any]:
    args = arguments or {}
    try:
        if name == "get_guide":
            from app.app.services import agent_guide

            return _tool_result({"version": agent_guide.GUIDE_VERSION, "guide_markdown": agent_guide.guide_markdown(project_id, agent_guide.project_name(db, project_id))})
        if name == "project_overview":
            return _tool_result(data.mcp_project_overview(db, settings, project_id))
        if name == "list_artifacts":
            return _tool_result(
                data.mcp_list_artifacts(
                    db,
                    project_id,
                    status=args.get("status"),
                    limit=int(args.get("limit") or 100),
                    prefix=args.get("prefix"),
                    settings=settings,
                    updated_since=args.get("updated_since"),
                ),
                compact=True,
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
                    cursor=args.get("cursor"),
                    limit=int(args["limit"]) if args.get("limit") else None,
                ),
                compact=artifact_id.startswith("contract/"),
            )
        if name == "get_requirements":
            return _tool_result(
                contract_tools.get_requirements(
                    db, settings, project_id, status=args.get("status"), updated_since=args.get("updated_since"),
                    requirement_ids=args.get("requirement_ids"), limit=int(args.get("limit") or 100), cursor=args.get("cursor"),
                    fields=args.get("fields"),
                ),
                compact=True,
            )
        if name in ("submit_question", "submit_verdicts"):
            from app.app.services import agent_gateway as gw

            body = {"question": str(args.get("question") or ""), "requirement_ids": args.get("requirement_ids") or []} if name == "submit_question" else {"items": args.get("items")}
            try:
                out = gw.submit_via_mcp(db, settings, project_id, token, "question" if name == "submit_question" else "verdicts",
                                        request_id=args.get("request_id"), body=body, client_request_id=args.get("client_request_id"))
            except PermissionError as exc:
                return _tool_error(str(exc), tool=name, detail=getattr(exc, "code", "read_only_key"))
            except ValueError as exc:
                return _tool_error(str(exc), tool=name)
            return _tool_result(out, compact=True)
        if name == "wait_reply":
            from app.app.services import agent_gateway as gw

            rid = str(args.get("request_id") or "")
            if not rid:
                return _tool_error("request_id обязателен", tool=name)
            return _tool_result(gw.wait_reply(db, project_id, rid, float(args.get("wait_s") or 30)), compact=True)
        if name == "exchange_status":
            from app.app.services import agent_gateway as gw

            return _tool_result(gw.exchange_status(db, project_id))
        if name == "get_changes":
            return _tool_result(contract_tools.get_changes(db, settings, project_id, since=args.get("since"), limit=int(args.get("limit") or 200)), compact=True)
        if name == "get_code_anchors":
            return _tool_result(contract_tools.get_code_anchors(db, settings, project_id, requirement_ids=args.get("requirement_ids"), system=args.get("system")), compact=True)
        if name == "get_conflicts":
            return _tool_result(contract_tools.get_conflicts(db, settings, project_id, min_severity=args.get("min_severity"), status=args.get("status")), compact=True)
        if name == "get_quote":
            if not args.get("requirement_id") and args.get("entity_id") is None:
                return _tool_error("requirement_id or entity_id is required", tool=name)
            return _tool_result(contract_tools.get_quote(db, settings, project_id, requirement_id=args.get("requirement_id"), entity_id=args.get("entity_id")))
        if name == "get_artifact_meta":
            artifact_id = str(args.get("artifact_id") or "").strip()
            if not artifact_id:
                return _tool_error("artifact_id is required", tool=name)
            return _tool_result(contract_tools.get_artifact_meta(db, settings, project_id, artifact_id=artifact_id))
        if name == "get_tz":
            return _tool_result(contract_tools.get_tz(db, settings, project_id, version=args.get("version")), compact=True)
        return _tool_error(f"Unknown tool: {name}", tool=name)
    except ValueError as exc:
        return _tool_error(str(exc), tool=name)
    except Exception as exc:
        return _tool_error("internal error", tool=name, detail=f"{type(exc).__name__}: {exc}")


def _instructions(db: Session, project_id: int) -> str:
    from app.app.services import agent_guide

    return agent_guide.short_instructions(project_id, agent_guide.project_name(db, project_id))


def handle_jsonrpc(
    db: Session,
    settings: Settings,
    project_id: int,
    message: dict[str, Any],
    token: Any = None,
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
                "instructions": _instructions(db, project_id),
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        tool_name = params.get("name")
        result = call_tool(db, settings, project_id, str(tool_name), params.get("arguments") or {}, token=token)
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
    token: Any = None,
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
        resp = handle_jsonrpc(db, settings, project_id, message, token=token)
        if resp is not None:
            responses.append(resp)
    return responses
