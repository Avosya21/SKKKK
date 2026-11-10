"""Клиент Jira: режим облака и своего сервера, постраничное чтение, разбор задачи, понятные ошибки. Сети нет — httpx.MockTransport."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx
import pytest

from app.app.services import jira_client as jc


def _issue(n: int, *, category: str = "new", assignee: str | None = "Тестовый Исполнитель", due: str | None = "2026-12-01") -> dict:
    return {"key": f"ABC-{n}", "fields": {
        "summary": f" Задача {n} ", "issuetype": {"name": "Задача"}, "priority": {"name": "Высокий"},
        "status": {"name": "Статус", "statusCategory": {"key": category}},
        "assignee": {"displayName": assignee} if assignee else None, "reporter": {"name": "reporter"},
        "duedate": due, "created": "2026-11-01T10:00:00.000+0300", "updated": "2026-11-02T10:00:00.000+0300",
        "labels": ["портал"], "parent": {"key": "ABC-1"}}}


def _client(handler, *, email: str = "") -> jc.JiraClient:
    return jc.JiraClient("https://jira.example/", "secret", email=email, transport=httpx.MockTransport(handler))


def test_not_configured_and_bad_url():
    with pytest.raises(jc.JiraNotConfigured):
        jc.JiraClient("", "token")
    with pytest.raises(jc.JiraNotConfigured):
        jc.JiraClient("https://jira.example", "")
    with pytest.raises(jc.JiraNotConfigured):
        jc.JiraClient("jira.example", "token")


def test_server_mode_uses_bearer_and_offset_paging():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        start = int(request.url.params["startAt"])
        page = [_issue(n) for n in range(start + 1, min(start + 100, 150) + 1)]
        return httpx.Response(200, json={"issues": page, "total": 150})

    client = _client(handler)
    issues = client.issues("abc")
    assert client.mode == "свой сервер" and len(issues) == 150
    assert [r.url.path for r in seen] == ["/rest/api/2/search", "/rest/api/2/search"]
    assert seen[0].headers["authorization"] == "Bearer secret"
    assert seen[0].url.params["jql"] == 'project = "ABC" ORDER BY updated DESC'
    assert seen[1].url.params["startAt"] == "100"


def test_cloud_mode_uses_basic_and_page_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "nextPageToken" not in request.url.params:
            return httpx.Response(200, json={"issues": [_issue(1), _issue(2)], "nextPageToken": "t2"})
        return httpx.Response(200, json={"issues": [_issue(3)], "isLast": True})

    client = _client(handler, email="user@example.com")
    issues = client.issues("ABC", updated_since="2026-10-01")
    assert client.mode == "облако" and [row["key"] for row in issues] == ["ABC-1", "ABC-2", "ABC-3"]
    assert all(r.url.path == "/rest/api/2/search/jql" for r in seen)
    assert seen[0].headers["authorization"] == "Basic " + base64.b64encode(b"user@example.com:secret").decode()
    assert 'updated >= "2026-10-01"' in seen[0].url.params["jql"]
    assert seen[1].url.params["nextPageToken"] == "t2"


def test_limit_stops_early():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"issues": [_issue(n) for n in range(1, int(request.url.params["maxResults"]) + 1)], "total": 1000})

    assert len(_client(handler).issues("ABC", limit=7)) == 7
    assert len(calls) == 1 and calls[0].url.params["maxResults"] == "7"


def test_normalize_issue_and_status_mapping():
    row = jc.normalize_issue(_issue(5, category="indeterminate"), "https://jira.example/")
    assert row == {"key": "ABC-5", "summary": "Задача 5", "type": "Задача", "status": "Статус", "portal_status": "in_progress",
                   "assignee": "Тестовый Исполнитель", "reporter": "reporter", "priority": "Высокий", "deadline_at": "2026-12-01",
                   "created": "2026-11-01T10:00:00.000+0300", "updated": "2026-11-02T10:00:00.000+0300", "labels": ["портал"],
                   "parent": "ABC-1", "url": "https://jira.example/browse/ABC-5"}
    empty = jc.normalize_issue({"key": "ABC-6", "fields": {"summary": None, "status": None, "assignee": None, "duedate": None}})
    assert empty["portal_status"] == "open" and empty["assignee"] is None and empty["deadline_at"] is None and empty["url"] is None
    assert jc.portal_status({"statusCategory": {"key": "done"}}) == "done"
    assert jc.portal_status({"statusCategory": {"key": "неизвестно"}}) == "open"


def test_myself_and_projects():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/myself"):
            return httpx.Response(200, json={"displayName": "Служебная учётка", "name": "svc"})
        return httpx.Response(200, json=[{"key": "ZZZ", "name": "Последний"}, {"key": "ABC", "name": "Первый", "projectTypeKey": "software"}])

    client = _client(handler)
    assert client.myself() == {"name": "Служебная учётка", "account": "svc"}
    assert [p["key"] for p in client.projects()] == ["ABC", "ZZZ"]


@pytest.mark.parametrize("status, text", [(401, "не приняла ключ"), (403, "отказала в доступе"), (404, "не нашла"),
                                          (429, "ограничила частоту"), (500, "вернула ошибку 500")])
def test_http_errors_are_readable(status, text):
    with pytest.raises(jc.JiraError, match=text):
        _client(lambda request: httpx.Response(status, text="oops")).myself()


def test_network_and_not_json_errors():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    with pytest.raises(jc.JiraError, match="Нет связи"):
        _client(boom).myself()
    with pytest.raises(jc.JiraError, match="не JSON"):
        _client(lambda request: httpx.Response(200, text="<html>login</html>")).myself()


def test_bad_project_key_and_date_never_reach_jira():
    client = _client(lambda request: pytest.fail("запрос не должен уйти"))
    with pytest.raises(jc.JiraError, match="ключ проекта"):
        client.issues('ABC" OR project = "X')
    with pytest.raises(jc.JiraError, match="ГГГГ-ММ-ДД"):
        client.issues("ABC", updated_since="вчера")


def test_settings_helpers():
    settings = SimpleNamespace(jira_base_url=" https://jira.example ", jira_api_token="t", jira_email="", jira_verify_tls=True,
                               jira_project_keys="abc, DEF;abc,, ghi")
    assert jc.configured(settings) and jc.project_keys(settings) == ["ABC", "DEF", "GHI"]
    assert not jc.configured(SimpleNamespace(jira_base_url="", jira_api_token="t"))
    assert jc.JiraClient.from_settings(settings, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))).mode == "свой сервер"


def test_search_issue_detail_and_statuses():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search"):
            return httpx.Response(200, json={"issues": [_issue(1)], "total": 1})
        if path.endswith("/statuses"):
            return httpx.Response(200, json=[{"name": "Задача", "statuses": [
                {"name": "К выполнению", "statusCategory": {"key": "new"}}, {"name": "Готово", "statusCategory": {"key": "done"}}]}])
        raw = _issue(7)
        raw["fields"]["description"] = "Текст описания"
        raw["fields"]["comment"] = {"total": 3, "comments": [
            {"author": {"displayName": "Первый Автор"}, "created": "2026-11-01T10:00:00.000+0300", "body": "раз"},
            {"author": {"name": "second"}, "created": "2026-11-02T10:00:00.000+0300", "body": "два"}]}
        return httpx.Response(200, json=raw)

    client = _client(handler)
    assert [row["key"] for row in client.search("project = ABC")] == ["ABC-1"]
    detail = client.issue("abc-7", comments=1)
    assert detail["description"] == "Текст описания" and detail["comments_total"] == 3
    assert detail["comments"] == [{"author": "second", "created": "2026-11-02T10:00:00.000+0300", "body": "два"}]
    assert client.project_statuses("abc") == [
        {"issue_type": "Задача", "status": "К выполнению", "category": "new", "portal_status": "open"},
        {"issue_type": "Задача", "status": "Готово", "category": "done", "portal_status": "done"}]
    with pytest.raises(jc.JiraError, match="Пустой запрос"):
        client.search("  ")
    with pytest.raises(jc.JiraError, match="ключ задачи"):
        client.issue("ABC-1/../x")


def test_mcp_server_tools_and_protocol():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("jira_mcp", root / "tools" / "jira_mcp.py")
    mcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcp)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/myself"):
            return httpx.Response(200, json={"displayName": "Служебная учётка"})
        if path.endswith("/project"):
            return httpx.Response(200, json=[{"key": "ABC", "name": "Первый"}])
        return httpx.Response(200, json={"issues": [_issue(1), _issue(2, category="done", assignee=None, due=None)], "total": 2})

    client = _client(handler)
    assert "свой сервер" in mcp.call_tool("jira_status", {}, client) and "secret" not in mcp.call_tool("jira_status", {}, client)
    assert mcp.call_tool("jira_projects", {}, client) == "проектов: 1\nABC — Первый"
    text = mcp.call_tool("jira_issues", {"project": "ABC", "limit": "не число"}, client)
    assert "ABC-1 [Статус → open] Задача 1" in text and "всего 2: open 1, done 1; без исполнителя 1, без срока 1" in text
    with pytest.raises(ValueError):
        mcp.call_tool("jira_delete", {}, client)
    listed = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert [tool["name"] for tool in listed] == ["jira_status", "jira_projects", "jira_project_statuses", "jira_issues", "jira_search", "jira_issue"]
    assert mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})["result"]["serverInfo"]["name"] == "primaryanalitics-jira"
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
