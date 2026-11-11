"""Jira в портале: токен, выбор проектов, загрузка задач и вложений как материалов, обновление, доступ. Jira — подставная."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app.app.services import jira_client as jc
from app.app.services import jira_import as ji

BASE = "https://jira.example"


class FakeJira:
    """Подставная Jira: проекты по токенам, задачи, вложения. Считает запросы задач."""

    def __init__(self):
        self.visible = {"tok-manager": ["ABC", "XYZ"], "tok-user": ["ABC"], "tok-admin": ["ABC", "XYZ", "OLD"]}
        self.names = {"ABC": "Проект Альфа", "XYZ": "Проект Икс", "OLD": "Старый проект"}
        self.issues = {
            "ABC": {"ABC-1": self.issue("ABC-1", "Настроить обмен", "2026-11-01T10:00:00.000+0300", attachments=[
                        {"id": "501", "filename": "схема.txt", "size": 12, "created": "2026-11-01T09:00:00.000+0300",
                         "content": f"{BASE}/secure/attachment/501/schema.txt"},
                        {"id": "502", "filename": "дамп.zip", "size": 10, "created": "2026-11-01T09:00:00.000+0300",
                         "content": f"{BASE}/secure/attachment/502/dump.zip"}]),
                    "ABC-2": self.issue("ABC-2", "Сверить остатки", "2026-11-02T10:00:00.000+0300")},
            "XYZ": {"XYZ-1": self.issue("XYZ-1", "Первая задача", "2026-11-03T10:00:00.000+0300")},
            "OLD": {},
        }
        self.detail_calls: list[str] = []

    @staticmethod
    def issue(key, summary, updated, attachments=None):
        return {"key": key, "fields": {
            "summary": summary, "issuetype": {"name": "Задача"}, "priority": {"name": "Средний"},
            "status": {"name": "В работе", "statusCategory": {"key": "indeterminate"}},
            "assignee": {"displayName": "Тестовый Исполнитель"}, "reporter": {"displayName": "Тестовый Автор"},
            "duedate": None, "created": "2026-10-01T10:00:00.000+0300", "updated": updated, "labels": [],
            "project": {"key": key.split("-")[0], "name": "Проект"}, "description": f"Описание задачи {key}.",
            "comment": {"total": 1, "comments": [{"author": {"displayName": "Тестовый Автор"}, "created": "2026-10-02T10:00:00.000+0300",
                                                  "body": "Договорились сделать до конца месяца."}]},
            "attachment": attachments or [], "issuelinks": []}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        keys = self.visible.get(token)
        if keys is None:
            return httpx.Response(401, json={})
        path = request.url.path
        if path.endswith("/myself"):
            return httpx.Response(200, json={"displayName": f"Учётка {token.split(chr(45))[1]}"})
        if path.endswith("/project"):
            return httpx.Response(200, json=[{"key": k, "name": self.names[k]} for k in keys])
        if path.endswith("/search"):
            key = request.url.params["jql"].split('"')[1]
            if key not in keys:
                return httpx.Response(400, json={})
            rows = sorted(self.issues[key].values(), key=lambda r: r["fields"]["updated"], reverse=True)
            return httpx.Response(200, json={"issues": rows, "total": len(rows)})
        if "/issue/" in path:
            key = path.rsplit("/", 1)[1]
            self.detail_calls.append(key)
            return httpx.Response(200, json=self.issues[key.split("-")[0]][key])
        if "/secure/attachment/" in path:
            return httpx.Response(200, content=b"attachment text")
        return httpx.Response(404, json={})

    def factory(self, settings, token):
        return jc.JiraClient(BASE, token, transport=httpx.MockTransport(self.handler))


@pytest.fixture()
def jira(monkeypatch):
    fake = FakeJira()
    monkeypatch.setenv("JIRA_BASE_URL", BASE)
    from app.app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(ji, "make_client", fake.factory)
    for name in ("connect", "visible_projects", "import_projects", "client_for", "start_sync", "scheduled_pass", "_sync_one"):
        func = getattr(ji, name)
        defaults = list(func.__defaults__ or ())
        if defaults and defaults[-1] is not fake.factory:
            defaults[-1] = fake.factory
            monkeypatch.setattr(func, "__defaults__", tuple(defaults))
    # загрузка в тестах идёт сразу, а не в фоновом потоке
    monkeypatch.setattr(ji, "start_sync", lambda settings, link_ids, user_id, factory=fake.factory: [ji._sync_one(settings, i, user_id, factory) for i in link_ids])
    yield fake
    get_settings.cache_clear()


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _artifacts(client, token, project_id):
    r = client.get(f"/projects/{project_id}/artifacts", headers=_h(token))
    assert r.status_code == 200, r.text
    return r.json()


def test_token_is_encrypted_and_never_returned(client, jira, manager_token):
    r = client.post("/jira/connect", json={"token": "tok-manager"}, headers=_h(manager_token))
    assert r.status_code == 200 and r.json() == {"connected": True, "name": "Учётка manager", "projects": 2}
    state = client.get("/jira/status", headers=_h(manager_token)).json()
    assert state["available"] and state["connected"] and state["can_import"] and not state["can_link_existing"]
    assert "tok-manager" not in str(state)
    from app.app.db import SessionLocal
    from app.app.models.jira import JiraAccount

    with SessionLocal() as db:
        account = db.scalar(select(JiraAccount))
        assert "tok-manager" not in account.token_enc
        from app.app.config import get_settings

        assert ji.decrypt_token(get_settings(), account.token_enc) == "tok-manager"
    assert client.post("/jira/connect", json={"token": "wrong"}, headers=_h(manager_token)).status_code == 400
    assert client.delete("/jira/connect", headers=_h(manager_token)).status_code == 204
    assert not client.get("/jira/status", headers=_h(manager_token)).json()["connected"]


def test_import_creates_project_and_materials_like_uploads(client, jira, manager_token):
    client.post("/jira/connect", json={"token": "tok-manager"}, headers=_h(manager_token))
    listed = client.get("/jira/projects", headers=_h(manager_token)).json()["projects"]
    assert [(p["key"], p["linked"]) for p in listed] == [("ABC", None), ("XYZ", None)]
    r = client.post("/jira/import", json={"projects": [{"key": "ABC"}]}, headers=_h(manager_token))
    assert r.status_code == 200, r.text
    project_id = r.json()["started"][0]["project_id"]
    projects = {p["id"]: p for p in client.get("/projects", headers=_h(manager_token)).json()}
    assert projects[project_id]["name"] == "Проект Альфа" and projects[project_id]["jira_key"] == "ABC"
    arts = _artifacts(client, manager_token, project_id)
    names = sorted(a["original_filename"] for a in arts)
    assert names == ["Jira ABC-1 — Настроить обмен.md", "Jira ABC-1 — схема.txt", "Jira ABC-2 — Сверить остатки.md"]
    assert all(a["status"] == "uploaded" and a["source_type"] == "document" for a in arts)  # дальше — общий конвейер
    state = client.get(f"/projects/{project_id}/jira", headers=_h(manager_token)).json()["links"][0]
    assert state["status"] == "idle" and state["issues"] == 2 and state["files"] == 1 and "пропущено 1" in state["status_text"]
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.job import Job

    with SessionLocal() as db:
        doc = db.scalar(select(Artifact).where(Artifact.original_filename == "Jira ABC-1 — Настроить обмен.md"))
        text = open(doc.file_path, encoding="utf-8").read()
        assert "# [ABC-1] Настроить обмен" in text and "Статус: В работе" in text and "Договорились сделать до конца месяца." in text
        assert f"Ссылка: {BASE}/browse/ABC-1" in text and "схема.txt" in text
        assert db.scalar(select(Job).where(Job.artifact_id == doc.id)).kind == "process_artifact"
        assert doc.recorded_at is not None and doc.recorded_at.year == 2026 and doc.recorded_at.month == 11


def test_update_replaces_changed_issue_and_skips_unchanged(client, jira, manager_token):
    client.post("/jira/connect", json={"token": "tok-manager"}, headers=_h(manager_token))
    project_id = client.post("/jira/import", json={"projects": [{"key": "ABC"}]}, headers=_h(manager_token)).json()["started"][0]["project_id"]
    before = {a["original_filename"]: a["artifact_id"] for a in _artifacts(client, manager_token, project_id)}
    jira.detail_calls.clear()
    jira.issues["ABC"]["ABC-2"] = jira.issue("ABC-2", "Сверить остатки", "2026-11-05T12:00:00.000+0300")
    jira.issues["ABC"]["ABC-2"]["fields"]["description"] = "Описание изменили."
    jira.issues["ABC"]["ABC-3"] = jira.issue("ABC-3", "Новая задача", "2026-11-05T13:00:00.000+0300")
    r = client.post(f"/projects/{project_id}/jira/sync", headers=_h(manager_token))
    assert r.status_code == 200, r.text
    assert sorted(jira.detail_calls) == ["ABC-2", "ABC-3"]  # неизменённую ABC-1 не перечитываем
    after = {a["original_filename"]: a["artifact_id"] for a in _artifacts(client, manager_token, project_id)}
    assert len(after) == 4 and "Jira ABC-3 — Новая задача.md" in after
    assert after["Jira ABC-1 — Настроить обмен.md"] == before["Jira ABC-1 — Настроить обмен.md"]
    assert after["Jira ABC-2 — Сверить остатки.md"] != before["Jira ABC-2 — Сверить остатки.md"]  # прежний документ заменён
    text = client.get(f"/projects/{project_id}/jira", headers=_h(manager_token)).json()["links"][0]["status_text"]
    assert "новых 1, обновлено 1, без изменений 1" in text


def test_access_follows_jira_visibility(client, jira, manager_token, user_token):
    client.post("/jira/connect", json={"token": "tok-manager"}, headers=_h(manager_token))
    started = client.post("/jira/import", json={"projects": [{"key": "ABC"}, {"key": "XYZ"}]}, headers=_h(manager_token)).json()["started"]
    abc, xyz = started[0]["project_id"], started[1]["project_id"]
    assert client.get("/projects", headers=_h(user_token)).json() == []  # без токена чужих проектов не видно
    assert client.get(f"/projects/{abc}", headers=_h(user_token)).status_code == 404
    client.post("/jira/connect", json={"token": "tok-user"}, headers=_h(user_token))
    assert [p["id"] for p in client.get("/projects", headers=_h(user_token)).json()] == [abc]  # ABC видит в Jira, XYZ — нет
    assert client.get(f"/projects/{xyz}/jira", headers=_h(user_token)).status_code == 404
    assert client.post("/jira/import", json={"projects": [{"key": "ABC"}]}, headers=_h(user_token)).status_code == 403
    jira.visible["tok-user"] = []  # в Jira доступ отозвали
    from app.app.config import get_settings

    ji.scheduled_pass(get_settings(), jira.factory)
    assert client.get("/projects", headers=_h(user_token)).json() == []
    jira.visible["tok-user"] = ["ABC"]
    client.get("/jira/projects", headers=_h(user_token))
    assert len(client.get("/projects", headers=_h(user_token)).json()) == 1
    client.delete("/jira/connect", headers=_h(user_token))
    assert client.get("/projects", headers=_h(user_token)).json() == []


def test_merge_into_existing_is_admin_only_and_key_is_unique(client, jira, admin_token, manager_token):
    existing = client.post("/projects", json={"name": "Уже есть", "internal": True}, headers=_h(admin_token)).json()["id"]
    client.post("/jira/connect", json={"token": "tok-manager"}, headers=_h(manager_token))
    r = client.post("/jira/import", json={"projects": [{"key": "XYZ", "project_id": existing}]}, headers=_h(manager_token))
    assert r.status_code == 400 and "администратор" in r.json()["detail"]
    r = client.post("/jira/import", json={"projects": [{"key": "OLD"}]}, headers=_h(manager_token))
    assert r.status_code == 400 and "не видит" in r.json()["detail"]
    client.post("/jira/connect", json={"token": "tok-admin"}, headers=_h(admin_token))
    r = client.post("/jira/import", json={"projects": [{"key": "XYZ", "project_id": existing}]}, headers=_h(admin_token))
    assert r.status_code == 200 and r.json()["started"][0]["project_id"] == existing
    assert [a["original_filename"] for a in _artifacts(client, admin_token, existing)] == ["Jira XYZ-1 — Первая задача.md"]
    other = client.post("/projects", json={"name": "Другой", "internal": True}, headers=_h(admin_token)).json()["id"]
    assert client.put(f"/projects/{other}/jira/link", json={"jira_key": "XYZ"}, headers=_h(admin_token)).status_code == 409
    assert client.put(f"/projects/{other}/jira/link", json={"jira_key": "old"}, headers=_h(manager_token)).status_code == 403
    assert client.put(f"/projects/{other}/jira/link", json={"jira_key": "old"}, headers=_h(admin_token)).json()["jira_key"] == "OLD"
    # менеджер, чей токен видит XYZ, получил доступ к существующему проекту
    assert existing in [p["id"] for p in client.get("/projects", headers=_h(manager_token)).json()]
    assert client.delete(f"/projects/{existing}/jira/link/XYZ", headers=_h(admin_token)).status_code == 204
    assert existing not in [p["id"] for p in client.get("/projects", headers=_h(manager_token)).json()]
    assert len(_artifacts(client, admin_token, existing)) == 1  # материалы остаются
    assert client.delete(f"/projects/{other}", headers=_h(admin_token)).status_code in (200, 204)  # удаление проекта с привязкой


def test_jira_hidden_without_server_address_and_markdown_shape(client, manager_token, monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "")
    from app.app.config import get_settings

    get_settings.cache_clear()
    try:
        assert client.get("/jira/status", headers=_h(manager_token)).json()["available"] is False
        assert client.post("/jira/connect", json={"token": "x"}, headers=_h(manager_token)).status_code == 400
    finally:
        get_settings.cache_clear()
    raw = FakeJira.issue("ABC-9", "Заголовок", "2026-11-02T10:00:00.000+0300")
    raw["fields"]["issuelinks"] = [{"type": {"outward": "блокирует"}, "outwardIssue": {"key": "ABC-10", "fields": {"summary": "Другая"}}}]
    text = ji.issue_markdown(raw, BASE, "Проект Альфа")
    assert text.startswith("# [ABC-9] Заголовок\n") and "проект ABC (Проект Альфа)" in text and "- блокирует ABC-10 — Другая" in text
    assert "## Комментарии (1)" in text and "### Тестовый Автор, 2026-10-02 10:00" in text
    assert ji.safe_filename('a/b:c*?"<>|d.') == "a b c d" and ji.jira_time("2026-11-02T10:00:00.000+0300").utcoffset().total_seconds() == 10800
