"""Проектный MCP по JSON-RPC: инструменты чтения знаний проекта и приёма данных агента, разбор запросов и инструкции клиенту."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.app.config import Settings
from app.app.services import project_mcp_contract as contract_tools
from app.app.services import project_mcp_data as data

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "primaryanalitics-project"
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
        "description": "Поиск по смыслу и словам во фрагментах проекта: текст фрагмента до 800 символов, кто (speaker) и когда (occurred_at); полностью — get_artifact_content(kind=text) с курсором.",
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
        "description": "Извлечённые сущности (решения, задачи, факты) по материалам проекта. Порциями {total, cursor, items}: читай до cursor = null.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_type": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100, "description": "Сколько в порции"},
                "cursor": {"type": "string", "description": "Из прошлого ответа: следующая порция"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_knowledge_graph",
        "description": "Узлы и связи графа знаний проекта порциями по узлам: в порции узлы и выходящие из них рёбра; читай до cursor = null.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200, "description": "Узлов в порции"}, "cursor": {"type": "string", "description": "Из прошлого ответа: следующая порция"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_notes",
        "description": "Пользовательские заметки по проекту, порциями {total, cursor, items}.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}, "cursor": {"type": "string", "description": "Из прошлого ответа: следующая порция"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_conflicts",
        "description": "Потенциальные конфликты между сущностями и их статус; сущности — разобранным JSON с verification. Порциями {total, cursor, items}.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}, "cursor": {"type": "string", "description": "Из прошлого ответа: следующая порция"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_artifact_content",
        "description": "Содержимое материала порциями: summary (выжимка), brief и outline (у созвона), text (стенограмма созвона, текст документа или переписки), messages (сообщения переписки), entities. auto — выжимка, иначе текст. Ответ: content, offset, total_chars, cursor (следующая порция или null), truncated, available_kinds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "minLength": 1},
                "kind": {
                    "type": "string",
                    "enum": ["auto", "summary", "brief", "outline", "text", "transcript", "messages", "entities"],
                    "default": "auto",
                },
                "cursor": {"type": "string", "description": "Из прошлого ответа: следующая порция (текста материала или файла контракта)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Для contract/*.json: пунктов в порции"},
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
        "name": "files_catalog",
        "description": "ФАЙЛЫ ПРОЕКТА (читает любой ключ проекта): карты конфигурации, дипмапы и другие посылки, принятые целиком — состав, размеры, sha256, README каждой посылки и текущий набор файлов (свежая версия каждого пути: посылки досылаются частями). Начни с него, если нужны файлы; скачивание — HTTP GET <адрес MCP>/files/bundles/<bundle_id>/data/<path> или files_get_chunk.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "files_open_bundle",
        "description": "ФАЙЛЫ (нужно право ключа «приём и чтение файлов»): открыть посылку по манифесту — относительные пути, размер, sha256 каждого файла. Повтор того же манифеста с тем же именем вернёт состояние для докачки. Крупные части удобнее слать по HTTP: PUT <адрес MCP>/files/bundles/<bundle_id>/data/<path>?offset=N, бинарное тело до 64 МБ, тот же Bearer-ключ, заголовок X-Chunk-Sha256.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 64, "description": "Латиница, цифры, . _ -; новая версия — новое имя"},
                "kind": {"type": "string", "enum": ["config_map", "entity_deepmap", "other"]},
                "note": {"type": "string", "maxLength": 2000},
                "files": {"type": "array", "maxItems": 10000, "items": {"type": "object", "properties": {"path": {"type": "string"}, "size": {"type": "integer"}, "sha256": {"type": "string"}}, "required": ["path", "size", "sha256"]}},
            },
            "required": ["name", "files"],
            "additionalProperties": False,
        },
    },
    {
        "name": "files_put_chunk",
        "description": "ФАЙЛЫ: часть файла посылки в base64 (до 1 МБ после декодирования) с позиции offset. offset должен равняться уже принятому (received); после обрыва бери received из files_bundle_status. Файл принят, когда sha256 целиком совпал с манифестом.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bundle_id": {"type": "integer"}, "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                "data_base64": {"type": "string"}, "chunk_sha256": {"type": "string", "description": "Необязательно: sha256 части"},
            },
            "required": ["bundle_id", "path", "offset", "data_base64"],
            "additionalProperties": False,
        },
    },
    {
        "name": "files_bundle_status",
        "description": "ФАЙЛЫ (право чтения файлов): манифест и состояние посылки; files=missing — только непринятые, с received (откуда продолжать).",
        "inputSchema": {
            "type": "object",
            "properties": {"bundle_id": {"type": "integer"}, "files": {"type": "string", "enum": ["all", "missing", "none"], "default": "missing"}},
            "required": ["bundle_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "files_list_bundles",
        "description": "ФАЙЛЫ (право чтения файлов): посылки проекта. Ключу только на чтение видны посылки, принятые целиком.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "files_get_chunk",
        "description": "ФАЙЛЫ (право чтения файлов): часть принятого файла в base64, до 1 МБ, с позиции offset. Целиком файлы удобнее скачивать по HTTP: GET <адрес MCP>/files/bundles/<bundle_id>/data/<path> (поддерживает Range, заголовок X-Sha256).",
        "inputSchema": {
            "type": "object",
            "properties": {"bundle_id": {"type": "integer"}, "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0, "default": 0},
                           "length": {"type": "integer", "minimum": 1, "maximum": 1048576}},
            "required": ["bundle_id", "path"],
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
        "description": "Дельта контракта после момента since: какие требования/решения/расхождения добавились, изменились, исчезли. Порциями: cursor из прошлого ответа.",
        "inputSchema": {
            "type": "object",
            "properties": {"since": {"type": "string", "description": "ISO-8601"}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200}, "cursor": {"type": "string", "description": "Из прошлого ответа: следующая порция"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_code_anchors",
        "description": "Только точки входа в код (1С/Битрикс24) по требованиям — гипотезы, которые агент анализа проверяет.",
        "inputSchema": {
            "type": "object",
            "properties": {"requirement_ids": {"type": "array", "items": {"type": "string"}}, "system": {"type": "string", "enum": ["1c", "b24"]}, "cursor": {"type": "string", "description": "Из прошлого ответа: следующая порция"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_conflicts",
        "description": "Расхождения контракта с двумя цитатами (сейчас правило scope: согласовано, но вне текущего ТЗ).",
        "inputSchema": {
            "type": "object",
            "properties": {"min_severity": {"type": "string", "enum": ["low", "medium", "high"]}, "status": {"type": "string"}, "cursor": {"type": "string", "description": "Из прошлого ответа: следующая порция"}},
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
        "name": "get_decisions",
        "description": "ПАМЯТЬ РЕШЕНИЙ: что решили по предметам обсуждения, кто и когда это сказал (дословные цитаты, сторона), что отклонили или заменили. Без параметров — предметы с состоянием и итогом; query — предметы по смыслу вопроса; subject_id — лента реплик и журнал предмета; as_of=ГГГГ-ММ-ДД — итог на дату. human=true — итог поправил человек. Только если память проекта включена (иначе ошибка memory_not_live). Порциями: cursor.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject_id": {"type": "integer"},
                "as_of": {"type": "string", "description": "ГГГГ-ММ-ДД: состояние на конец этого дня"},
                "query": {"type": "string", "maxLength": 300, "description": "О чём вопрос: найдёт подходящие предметы"},
                "state": {"type": "string", "enum": ["agreed", "documented", "proposed", "open", "rejected", "superseded", "implemented", "withdrawn"]},
                "cursor": {"type": "string", "description": "Из прошлого ответа: следующая порция"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_tz",
        "description": "Текущее ТЗ по пунктам с привязкой к требованиям (requirement_ids).",
        "inputSchema": {"type": "object", "properties": {"version": {"type": "string"}}, "additionalProperties": False},
    },
]


# Любой инструмент принимает page_bytes: размер порции ответа (MCP-KB-PLAN В1; по умолчанию MCP_PAGE_BYTES = 60 КБ, до 200 КБ).
PAGE_BYTES_SCHEMA = {"type": "integer", "minimum": 8000, "maximum": 200000,
                     "description": "Размер порции ответа в байтах (по умолчанию 60000 ≈ 20 тыс. токенов; больше — если клиент принимает длинные ответы)"}
for _tool in MCP_TOOLS:
    _tool["inputSchema"].setdefault("properties", {})["page_bytes"] = PAGE_BYTES_SCHEMA


def _tool_result(payload: Any, *, compact: bool = True) -> dict[str, Any]:
    """Ответ инструмента — всегда компактный JSON: отступы стоили до трети токенов."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
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
    """Вызов инструмента в бюджете порции (`page_bytes` агента или MCP_PAGE_BYTES) и со стражем размера ответа."""
    from app.app.services import mcp_page

    args = dict(arguments or {})
    page = mcp_page.clamp(args.pop("page_bytes", None), getattr(settings, "mcp_page_bytes", None) or 60_000)
    with mcp_page.budget(page):
        result = _call_tool(db, settings, project_id, name, args, token)
    return _guard_size(result, page, name, args)


def _guard_size(result: dict[str, Any], page: int, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Страж: ответ больше порции не уходит агенту целиком (у клиентов лимит ответа инструмента). Список — первая порция с
    курсором; иное — понятная ошибка с подсказкой. Инструменты сами режут свои ответы; сюда попадает только непредусмотренное."""
    from app.app.services import mcp_page

    text = result["content"][0]["text"]
    if result.get("isError") or len(text.encode("utf-8")) <= page * 1.05:
        return result
    payload = json.loads(text)
    if isinstance(payload, list):
        return _tool_result(mcp_page.list_page({"items": payload}, "items", args.get("cursor"), max_bytes=page))
    return _tool_error("ответ больше порции", tool=name,
                       detail=f"too_large: {len(text.encode('utf-8'))} байт при порции {page}; уменьши limit или увеличь page_bytes (до 200000)")


def _call_tool(db: Session, settings: Settings, project_id: int, name: str, args: dict[str, Any], token: Any) -> dict[str, Any]:
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
                    text_chars=800,
                    max_bytes=_page(),
                )
            )
        if name == "list_entities":
            return _tool_result(
                data.mcp_list_entities(
                    db,
                    project_id,
                    entity_type=args.get("entity_type"),
                    limit=int(args.get("limit") or 100),
                    cursor=args.get("cursor"),
                )
            )
        if name == "get_knowledge_graph":
            return _tool_result(
                data.mcp_get_knowledge_graph(db, project_id, limit=int(args.get("limit") or 200), cursor=args.get("cursor"))
            )
        if name == "list_notes":
            return _tool_result(data.mcp_list_notes(db, project_id, limit=int(args.get("limit") or 50), cursor=args.get("cursor")))
        if name == "list_conflicts":
            return _tool_result(data.mcp_list_conflicts(db, project_id, limit=int(args.get("limit") or 50), cursor=args.get("cursor")))
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
        if name.startswith("files_"):
            return _files_tool(db, settings, project_id, name, args, token)
        if name == "exchange_status":
            from app.app.services import agent_gateway as gw

            return _tool_result(gw.exchange_status(db, project_id))
        if name == "get_changes":
            return _tool_result(contract_tools.get_changes(db, settings, project_id, since=args.get("since"), limit=int(args.get("limit") or 200), cursor=args.get("cursor")), compact=True)
        if name == "get_code_anchors":
            return _tool_result(contract_tools.get_code_anchors(db, settings, project_id, requirement_ids=args.get("requirement_ids"), system=args.get("system"), cursor=args.get("cursor")), compact=True)
        if name == "get_conflicts":
            return _tool_result(contract_tools.get_conflicts(db, settings, project_id, min_severity=args.get("min_severity"), status=args.get("status"), cursor=args.get("cursor")), compact=True)
        if name == "get_quote":
            if not args.get("requirement_id") and args.get("entity_id") is None:
                return _tool_error("requirement_id or entity_id is required", tool=name)
            return _tool_result(contract_tools.get_quote(db, settings, project_id, requirement_id=args.get("requirement_id"), entity_id=args.get("entity_id")))
        if name == "get_artifact_meta":
            artifact_id = str(args.get("artifact_id") or "").strip()
            if not artifact_id:
                return _tool_error("artifact_id is required", tool=name)
            return _tool_result(contract_tools.get_artifact_meta(db, settings, project_id, artifact_id=artifact_id))
        if name == "get_decisions":
            try:
                return _tool_result(data.mcp_get_decisions(db, settings, project_id, subject_id=args.get("subject_id"), as_of=args.get("as_of"),
                                                           query=args.get("query"), state=args.get("state"), cursor=args.get("cursor")))
            except PermissionError as exc:
                return _tool_error(str(exc), tool=name, detail="memory_not_live")
        if name == "get_tz":
            return _tool_result(contract_tools.get_tz(db, settings, project_id, version=args.get("version")), compact=True)
        return _tool_error(f"Unknown tool: {name}", tool=name)
    except ValueError as exc:
        return _tool_error(str(exc), tool=name)
    except Exception as exc:
        return _tool_error("internal error", tool=name, detail=f"{type(exc).__name__}: {exc}")


def _page() -> int:
    from app.app.services import mcp_page

    return mcp_page.current(mcp_page.MAX_PAGE_BYTES)


def _files_tool(db: Session, settings: Settings, project_id: int, name: str, args: dict[str, Any], token: Any) -> dict[str, Any]:
    """Посылки файлов через инструменты MCP (мелкими частями); тот же сервис, что и HTTP-путь."""
    import base64
    import binascii
    import hashlib

    from app.app.services import exchange_files as xf

    try:
        if name == "files_open_bundle":
            return _tool_result(xf.open_bundle(db, settings, project_id, token, name=args.get("name"), files=args.get("files"),
                                               kind=args.get("kind"), note=args.get("note")), compact=True)
        if name == "files_put_chunk":
            try:
                data = base64.b64decode(str(args.get("data_base64") or ""), validate=True)
            except (binascii.Error, ValueError):
                return _tool_error("data_base64: не base64", tool=name, detail="bad_request")
            return _tool_result(xf.put_chunk(db, settings, project_id, token, args.get("bundle_id"), args.get("path"), args.get("offset"),
                                             data, chunk_sha256=args.get("chunk_sha256"), max_chunk=xf.MCP_CHUNK_MAX), compact=True)
        if name == "files_bundle_status":
            xf.require_files(token, "read")
            files = args.get("files") or "missing"
            return _tool_result(xf.bundle_dict(db, xf.get_bundle(db, project_id, args.get("bundle_id")),
                                               files=files if files in ("all", "missing", "none") else "missing"), compact=True)
        if name == "files_catalog":
            return _tool_result(xf.catalog(db, settings, project_id, token), compact=True)
        if name == "files_list_bundles":
            xf.require_files(token, "read")
            return _tool_result({"items": xf.list_bundles(db, project_id, token)}, compact=True)
        if name == "files_get_chunk":
            f, data = xf.read_chunk(db, settings, project_id, token, args.get("bundle_id"), args.get("path"), args.get("offset") or 0,
                                    args.get("length") or xf.MCP_CHUNK_MAX)
            offset = int(args.get("offset") or 0)
            return _tool_result({"path": f.path, "size": f.size, "sha256": f.sha256, "offset": offset, "length": len(data),
                                 "eof": offset + len(data) >= f.size, "chunk_sha256": hashlib.sha256(data).hexdigest(),
                                 "data_base64": base64.b64encode(data).decode("ascii")}, compact=True)
    except PermissionError as exc:
        return _tool_error(str(exc), tool=name, detail=getattr(exc, "code", "files_not_allowed"))
    except xf.FilesError as exc:
        return _tool_error(str(exc), tool=name, detail=exc.code)
    return _tool_error(f"Unknown tool: {name}", tool=name)


def _instructions(db: Session, project_id: int, token: Any = None) -> str:
    """Инструкция при подключении: права именно этого ключа, порции, файлы проекта."""
    from app.app.services import agent_guide
    from app.app.services.project_mcp_tokens import token_files_access, token_scope

    scope, files = (token_scope(token), token_files_access(token)) if token is not None else ("read", "read")
    text = agent_guide.short_instructions(project_id, agent_guide.project_name(db, project_id), scope, files)
    from app.app.services import exchange_files as xf

    brief = xf.catalog_brief(db, project_id) if db is not None else None
    if brief:  # агент узнаёт о файлах проекта при подключении, не зная о них заранее
        kinds = ", ".join(sorted({i["kind"] for i in brief["items"]}))
        text += (f" ФАЙЛЫ ПРОЕКТА: {brief['bundles']} посылок, {brief['files']} файлов, {brief['total_bytes'] / 1024 ** 2:.0f} МБ "
                 f"(виды: {kinds}) — состав, хеши и README в инструменте files_catalog; скачивание — по HTTP или files_get_chunk.")
    return text


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
                "instructions": _instructions(db, project_id, token),
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        import time

        from app.app.services.mcp_journal import record_call

        tool_name = str(params.get("name"))
        arguments = params.get("arguments") or {}
        started = time.monotonic()
        result = call_tool(db, settings, project_id, tool_name, arguments, token=token)
        text = result["content"][0]["text"] if result.get("content") else ""
        error = None
        if result.get("isError"):
            try:
                error = str(json.loads(text).get("detail") or json.loads(text).get("error") or "error")[:60]
            except (ValueError, AttributeError):
                error = "error"
        # журнал чтения (MCP-KB-PLAN Э4): имена параметров, размер и время — без содержимого
        record_call(project_id, token, tool_name, arguments if isinstance(arguments, dict) else {}, size=len(text.encode("utf-8")),
                    ms=int((time.monotonic() - started) * 1000), error=error)
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
