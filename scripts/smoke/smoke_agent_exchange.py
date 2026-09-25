"""Обмен с внешним агентом (шлюз + MCP-контракт) на живом сервере, только песочница: ключ создаётся и отзывается."""
import json

import httpx

from common import *

c, _ = api_login("admin")
tok = c.post(f"/projects/{PID}/mcp/tokens", json={"label": "smoke-agent-exchange"})
check(tok.status_code == 201, "временный MCP-ключ выпущен")
key = tok.json()["token"]
key_id = tok.json().get("id")
mcp = httpx.Client(base_url=BASE, verify=False, timeout=90, headers={"Authorization": f"Bearer {key}"})


def call(name, **args):
    r = mcp.post(f"/mcp/projects/{PID}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    r.raise_for_status()
    res = r.json()["result"]
    return res["isError"], json.loads(res["content"][0]["text"])


try:
    tools = mcp.post(f"/mcp/projects/{PID}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    names = {t["name"] for t in tools}
    check({"get_requirements", "get_changes", "get_code_anchors", "get_conflicts", "get_quote", "get_artifact_meta", "get_tz"} <= names, f"MCP отдаёт {len(names)} инструментов")
    init = mcp.post(f"/mcp/projects/{PID}", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).json()["result"]
    check("get_guide" in init["instructions"], "при подключении агент получает инструкцию (initialize.instructions)")
    err, g = call("get_guide")
    check(not err and "analysis_question" in g["guide_markdown"], "get_guide отдаёт руководство")
    err, arts = call("list_artifacts")
    check(not err and any(a["type"] == "contract" for a in arts), "list_artifacts работает и показывает файлы контракта")
    err, only = call("list_artifacts", prefix="contract/")
    check(not err and all(a["filename"].startswith("contract/") for a in only), "фильтр по префиксу contract/")
    err, page = call("get_artifact_content", artifact_id="contract/state.json")
    check(not err and page["content"]["schema_version"] == "1.0", "contract/state.json читается")
    err, reqs = call("get_requirements", limit=5)
    check(not err and "items" in reqs and reqs["total"] >= 0, f"get_requirements: {reqs['total']} требований")
    err, bad = call("get_quote")
    check(err and bad["tool"] == "get_quote" and "error" in bad, "ошибка отдаётся JSON-ом {error, tool, detail}")

    ids = [r["id"] for r in reqs["items"]][:2]
    body = json.dumps({"request_id": 901, "requirement_ids": ids or ["req-x-0001"]}).encode()
    up = c.post(f"/projects/{PID}/upload", files=[("files", ("analysis_request_901.json", body, "application/json"))], data={"source_type": "document"})
    check(up.status_code == 200, "запрос агента загружен")
    err, replies = call("list_artifacts", prefix="contract/kb_reply_")
    check(not err and any(a["filename"] == "contract/kb_reply_901.json" for a in replies), "ответ базы знаний виден как contract/kb_reply_901.json")

    evil = json.dumps({"schema_version": "1.0", "request_id": 902, "items": [{"requirement_id": "req-x-0001", "verdict": "implemented", "confidence": "high", "blockers": ["rm -rf C:\\1C_NEW_SKKKK"]}]}).encode()
    up2 = c.post(f"/projects/{PID}/upload", files=[("files", ("analysis_reply_902.json", evil, "application/json"))], data={"source_type": "document"})
    check(up2.status_code == 200 and up2.json()[0]["status"] == "rejected", f"вредоносный файл отклонён шлюзом (статус {up2.json()[0]['status']})")

    log = c.get(f"/projects/{PID}/agent-exchange").json()["items"]
    decisions = {(r["request_id"], r["decision"]) for r in log}
    check(("902", "rejected") in decisions and ("901", "accepted") in decisions, "оба обращения записаны в журнал с решениями")
finally:
    if key_id is not None:
        c.delete(f"/projects/{PID}/mcp/tokens/{key_id}")
