from __future__ import annotations

import json

from app.app.mcp.project_protocol import call_tool, dispatch_mcp_body, handle_jsonrpc
from app.app.services.project_mcp_tokens import create_project_mcp_token


def test_mcp_initialize_returns_tools_capability():
    resp = handle_jsonrpc(None, None, 1, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert resp["result"]["capabilities"]["tools"]
    assert resp["result"]["serverInfo"]["name"] == "skkkk-project"


def test_mcp_tools_list_has_base_and_contract_tools():
    resp = handle_jsonrpc(None, None, 1, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "project_overview" in names
    assert "search_project" in names
    base = {"project_overview", "list_artifacts", "get_project_summary", "search_project", "list_entities",
            "get_knowledge_graph", "list_notes", "list_conflicts", "get_artifact_content", "get_guide", "wait_reply", "exchange_status", "submit_question", "submit_verdicts",
            "files_open_bundle", "files_put_chunk", "files_bundle_status", "files_list_bundles", "files_get_chunk"}
    contract = {"get_requirements", "get_changes", "get_code_anchors", "get_conflicts", "get_quote", "get_artifact_meta", "get_tz"}
    assert base | contract == names


def test_create_and_auth_mcp_token(db_engine, client, admin_token, manager_token):
    from app.app.db import SessionLocal
    from app.app.services.project_mcp_tokens import authenticate_project_mcp_token

    headers = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "MCP Proj"}, headers=headers).json()["id"]

    r = client.post(
        f"/projects/{pid}/mcp/tokens",
        json={"label": "Test"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 201
    plain = r.json()["token"]
    assert plain.startswith("skkkk_mcp_")

    db = SessionLocal()
    assert authenticate_project_mcp_token(db, project_id=pid, bearer=plain) is not None
    db.close()

    mcp_r = client.post(
        f"/mcp/projects/{pid}",
        headers={"Authorization": f"Bearer {plain}", "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    )
    assert mcp_r.status_code == 200
    assert len(mcp_r.json()["result"]["tools"]) == 26


def test_mcp_rejects_without_token(client, manager_token):
    headers = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "MCP Deny"}, headers=headers).json()["id"]
    r = client.post(
        f"/mcp/projects/{pid}",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert r.status_code == 401


def test_mcp_project_overview_tool(client, db_engine, admin_token, manager_token):
    headers = {"Authorization": f"Bearer {manager_token}"}
    pid = client.post("/projects", json={"name": "MCP Overview"}, headers=headers).json()["id"]

    from app.app.db import SessionLocal

    db = SessionLocal()
    _, plain = create_project_mcp_token(db, project_id=pid, label="t", user_id=1)
    db.close()

    r = client.post(
        f"/mcp/projects/{pid}",
        headers={"Authorization": f"Bearer {plain}", "Content-Type": "application/json"},
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "project_overview", "arguments": {}},
        },
    )
    assert r.status_code == 200
    content = r.json()["result"]["content"][0]["text"]
    payload = json.loads(content)
    assert payload["project_id"] == pid
    assert payload["name"] == "MCP Overview"
