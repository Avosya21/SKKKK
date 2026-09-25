"""Руководство для внешних агентов: доходит до агента само (initialize, get_guide, contract/GUIDE.md) и не противоречит политике."""
from __future__ import annotations

import json

from app.app.services import agent_gateway as gw
from app.app.services import agent_guide as guide
from app.app.tests.test_contract import _seed


def test_guide_mentions_every_gateway_contract_and_limits():
    text = guide.guide_markdown(1, "П")
    for needle in ("analysis_request_<n>.json", "analysis_question_<n>.json", "analysis_reply_<n>.json", "kb_reply_<n>.json",
                   "200 КБ", "30 обращений", "get_quote", "verification", "unknown"):
        assert needle in text, needle
    for verdict in gw.VERDICTS | gw.CONFIDENCE:
        assert verdict in text, verdict
    for never in ("удаляем", "команды", "пароли"):
        assert never in text
    assert "{pid}" not in text and "{ver}" not in text and "{{" not in text


def test_short_instructions_point_to_guide():
    s = guide.short_instructions(18, "Проект")
    assert "get_guide" in s and "contract/GUIDE.md" in s and "#18" in s


def test_agent_gets_guide_through_mcp(client, manager_token, tmp_data):
    from app.app.config import get_settings
    from app.app.mcp.project_protocol import handle_jsonrpc
    from app.app.services import project_mcp_data as data

    db, pid, _art, _ = _seed(client, manager_token, tmp_data)
    settings = get_settings()
    init = handle_jsonrpc(db, settings, pid, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert "get_guide" in init["result"]["instructions"]
    names = {t["name"] for t in handle_jsonrpc(db, settings, pid, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert "get_guide" in names
    res = handle_jsonrpc(db, settings, pid, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_guide", "arguments": {}}})
    assert not res["result"]["isError"] and "analysis_question" in json.loads(res["result"]["content"][0]["text"])["guide_markdown"]
    files = data.mcp_list_artifacts(db, pid, prefix="contract/", settings=settings)
    assert files[0]["filename"] == "contract/GUIDE.md"
    page = data.mcp_get_artifact_content(db, settings, pid, artifact_id="contract/GUIDE.md")
    assert page["content"].startswith("# Как работать с базой знаний")


def test_guide_documents_reply_statuses_and_new_tools():
    text = guide.guide_markdown(1, "П")
    for needle in ("wait_reply", "exchange_status", "rejected", "needs_human", "fields=\"brief\"", "content_hash", "updated_since"):
        assert needle in text, needle
