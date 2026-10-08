"""MCP базы знаний: инструкция по правам ключа, сведения о подключении, журнал чтения (docs/plans/MCP-KB-PLAN.md, Э3–Э4)."""
from __future__ import annotations

import logging

from app.app.mcp.project_protocol import MCP_TOOLS
from app.app.services import agent_guide as guide


def _project_and_key(client, manager_token, admin_token, scope: str):
    h = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "MCP журнал"}, headers=h).json()["id"]
    r = client.post(f"/projects/{pid}/mcp/tokens", json={"label": "агент", "scope": scope, "agent_name": "анализ"},
                    headers={"Authorization": f"Bearer {admin_token}"})
    return pid, r.json()["token"]


def _rpc(client, pid, key, method, params=None):
    return client.post(f"/mcp/projects/{pid}", headers={"Authorization": f"Bearer {key}"},
                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).json()


def test_instructions_name_the_rights_of_this_key(client, manager_token, admin_token, tmp_data):
    pid, key = _project_and_key(client, manager_token, admin_token, "read")
    text = _rpc(client, pid, key, "initialize")["result"]["instructions"]
    assert "ТВОЙ КЛЮЧ: только чтение" in text and "analysis_question_<n>.json" in text and "cursor" in text
    pid2, key2 = _project_and_key(client, manager_token, admin_token, "questions_verdicts")
    text2 = _rpc(client, pid2, key2, "initialize")["result"]["instructions"]
    assert "submit_verdicts" in text2 and "только чтение" not in text2  # раньше всем: «Доступ только для чтения»


def test_guide_explains_pages_and_material_kinds():
    text = guide.guide_markdown(1, "П")
    for needle in ("page_bytes", "cursor", "available_kinds", "too_large", "kind", "messages"):
        assert needle in text, needle
    assert guide.GUIDE_VERSION == "1.3"


def test_mcp_info_counts_tools_and_does_not_claim_read_only(client, manager_token, tmp_data):
    h = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "MCP info"}, headers=h).json()["id"]
    info = client.get(f"/projects/{pid}/mcp/info", headers=h).json()
    assert info["tools_count"] == len(MCP_TOOLS) and info["read_only"] is False and "по правам ключа" in info["access"]


def test_every_call_is_journaled_without_content(client, manager_token, admin_token, tmp_data, caplog):
    pid, key = _project_and_key(client, manager_token, admin_token, "read")
    with caplog.at_level(logging.INFO, logger="skkkk.mcp"):
        _rpc(client, pid, key, "tools/call", {"name": "get_project_summary", "arguments": {}})
        _rpc(client, pid, key, "tools/call", {"name": "get_quote", "arguments": {"requirement_id": "секретная-фраза-поиска"}})
    lines = [r.getMessage() for r in caplog.records if "mcp.call" in r.getMessage()]
    assert len(lines) == 2 and "tool=get_project_summary" in lines[0] and "agent=анализ" in lines[0]
    assert "args=requirement_id" in lines[1] and "error=" in lines[1] and "секретная" not in lines[1]  # имена параметров, не значения
    tokens = client.get(f"/projects/{pid}/mcp/tokens", headers={"Authorization": f"Bearer {manager_token}"}).json()["tokens"]
    usage = tokens[0]["usage_7d"]
    assert usage["calls"] == 2 and usage["errors"] == 1 and set(usage["top_tools"]) == {"get_project_summary", "get_quote"}
