"""E2E-тест интеграции с Jira: подставная Jira — настоящий HTTP-сервер на 127.0.0.1 (реальный сокет, реальный клиент),
портал — через TestClient. Прогоняем connect → список проектов → импорт (фоновая синхронизация: md-документы и вложения
попадают на диск) → повторную синхронизацию с изменённой задачей (замена документа) и расписной проход ``scheduled_pass``.

Закрепляет, что вся информация Jira (поля, описание, комментарии, вложения) разложена в память проекта как материалы для
общего конвейера — docs/plans/JIRA-INTEGRATION-PLAN.md. В отличие от test_jira_import.py здесь не подменяются ни клиент
Jira, ни фоновый поток: httpx ходит по реальному сокету до собственного HTTP-сервера."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.app.services import jira_import as ji

TOKEN = "e2e-token"
PLAN_BYTES = "\u043f\u043b\u0430\u043d \u043d\u043e\u0440\u043c\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u0438\nRAL 6005\n".encode("utf-8")
ATTACH_PLAN = ("9001", "\u043f\u043b\u0430\u043d.txt", PLAN_BYTES, "2026-11-10T08:00:00.000+0300")
ATTACH_EPF = ("9002", "\u0432\u044b\u0433\u0440\u0443\u0437\u043a\u0430.epf", b"epf-binary", "2026-11-10T08:05:00.000+0300")


def _issue(key, summary, status_name, category, updated, description, comments, attachments):
    """Задача в сыром виде Jira: поля, описание, комментарии, вложения (id, имя, содержимое, дата)."""

    def att(a):
        aid, name, content, created = a
        return {"id": aid, "filename": name, "size": len(content), "created": created,
                "content": f"/secure/attachment/{aid}/{name}", "author": {"displayName": "E2E \u0410\u0432\u0442\u043e\u0440"}}

    comment_rows = [{"author": {"displayName": f"\u0410\u0432\u0442\u043e\u0442 {i}"}, "created": f"2026-11-09T1{i}:00:00.000+0300",
                     "body": body} for i, body in enumerate(comments)]
    return {"key": key, "self": f"/rest/api/2/issue/{key}", "fields": {
        "summary": summary, "issuetype": {"name": "\u0417\u0430\u0434\u0430\u0447\u0430"}, "priority": {"name": "\u0421\u0440\u0435\u0434\u043d\u0438\u0439"},
        "status": {"name": status_name, "statusCategory": {"key": category}},
        "assignee": {"displayName": "E2E \u0418\u0441\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c"}, "reporter": {"displayName": "E2E \u0410\u0432\u0442\u043e\u0440"},
        "duedate": None, "created": "2026-11-05T10:00:00.000+0300", "updated": updated, "labels": [],
        "project": {"key": key.split("-")[0], "name": "\u041f\u0440\u043e\u0435\u043a\u0442 E2E (\u0438\u0442\u043e\u0433\u043e\u0432\u044b\u0439)"},
        "description": description, "comment": {"total": len(comment_rows), "comments": comment_rows},
        "attachment": [att(a) for a in attachments], "issuelinks": []}}


class FakeJiraServer:
    """Подставная Jira по реальному HTTP: myself / project / search / issue/<key> / secure/attachment."""

    def __init__(self):
        self.base = ""  # адрес подставной Jira; ставит фикстура jira_e2e
        self.attachment_bytes = {aid: content for aid, _name, content, _created in (ATTACH_PLAN, ATTACH_EPF)}
        self.issues = {
            "E2E-1": _issue("E2E-1", "\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u043d\u043e\u0440\u043c\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u0438", "\u0413\u043e\u0442\u043e\u0432\u043e",
                            "done", "2026-11-10T09:30:00.000+0300", "\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 E2E: \u043d\u043e\u0440\u043c\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u044f \u043d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\u044b.",
                            ["\u0421\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u0430\u0442\u044c \u0441 \u0437\u0430\u043a\u0430\u0437\u0447\u0438\u043a\u043e\u043c \u0434\u043e \u043f\u044f\u0442\u043d\u0438\u0446\u044b."], [ATTACH_PLAN, ATTACH_EPF]),
            "E2E-2": _issue("E2E-2", "\u0412\u0442\u043e\u0440\u0430\u044f \u0437\u0430\u0434\u0430\u0447\u0430", "\u0412 \u0440\u0430\u0431\u043e\u0442\u0435",
                            "indeterminate", "2026-11-11T10:00:00.000+0300", "\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 \u0432\u0442\u043e\u0440\u043e\u0439 \u0437\u0430\u0434\u0430\u0447\u0438.", [], []),
        }

    def abs_urls(self, raw):
        """Jira отдаёт вложения абсолютными адресами: download() требует base_url + "/"."""
        for a in (raw.get("fields") or {}).get("attachment") or []:
            if not str(a["content"]).startswith(self.base + "/"):
                a["content"] = self.base + str(a["content"])

    def change(self, key, *, description=None, updated=None, add_comment=None):
        """Изменить задачу «в Jira»: так выглядит правка у заказчика между синхронизациями."""
        fields = self.issues[key]["fields"]
        if description is not None:
            fields["description"] = description
        if updated is not None:
            fields["updated"] = updated
        if add_comment:
            block = fields["comment"]
            block["comments"].append({"author": {"displayName": "\u0410\u0432\u0442\u043e\u0440-\u0434\u043e\u043f"}, "created": "2026-11-11T14:00:00.000+0300",
                                      "body": add_comment})
            block["total"] = len(block["comments"])


def _make_handler(server: FakeJiraServer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # тишина в логах pytest
            pass

        def _send(self, code, payload=b"", content_type="application/json"):
            self.send_response(code)
            if payload:
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def do_GET(self):  # noqa: N802 — конвенция http.server
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._send(401, b"{}")
            path = urlparse(self.path).path
            dumps = lambda obj: json.dumps(obj, ensure_ascii=False).encode()  # noqa: E731 — только внутри обработчика
            if path == "/rest/api/2/myself":
                return self._send(200, dumps({"displayName": "E2E \u0423\u0447\u0451\u0442\u043a\u0430", "emailAddress": "e2e@example.com"}))
            if path == "/rest/api/2/project":
                names = {}
                for k, v in server.issues.items():
                    names.setdefault(k.split("-")[0], v["fields"]["project"]["name"])
                rows = [{"key": k, "name": name, "projectTypeKey": "software"} for k, name in sorted(names.items())]
                return self._send(200, dumps(rows))
            if path == "/rest/api/2/search":
                jql = (parse_qs(urlparse(self.path).query).get("jql") or [""])[0]
                m = re.search(r'project\s*=\s*"([A-Za-z0-9_]+)"', jql)
                prefix = f"{m.group(1)}-" if m else ""
                rows = sorted(server.issues.values(), key=lambda r: str(r["fields"]["updated"]), reverse=True)
                rows = [r for r in rows if r["key"].startswith(prefix)]
                for row in rows:
                    server.abs_urls(row)
                return self._send(200, dumps({"issues": rows, "total": len(rows)}))
            if path.startswith("/rest/api/2/issue/"):
                raw = server.issues.get(path.rsplit("/", 1)[1])
                if raw is None:
                    return self._send(404, b"{}")
                server.abs_urls(raw)
                return self._send(200, dumps(raw))
            m = re.match(r"^/secure/attachment/(\d+)/(.+)$", path)
            if m and m.group(1) in server.attachment_bytes:
                return self._send(200, server.attachment_bytes[m.group(1)], "application/octet-stream")
            return self._send(404, b"not found", "text/plain")

    return Handler


@pytest.fixture()
def jira_e2e(monkeypatch):
    """Настоящий HTTP-сервер подставной Jira на случайном порту 127.0.0.1; адрес — в JIRA_BASE_URL."""
    server = FakeJiraServer()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(server))
    server.base = base = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    monkeypatch.setenv("JIRA_BASE_URL", base)
    from app.app.config import get_settings

    get_settings.cache_clear()
    yield server
    httpd.shutdown()
    httpd.server_close()
    get_settings.cache_clear()


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _artifacts(client, token, project_id):
    r = client.get(f"/projects/{project_id}/artifacts", headers=_h(token))
    assert r.status_code == 200, r.text
    return r.json()


def _wait_link(client, token, project_id, timeout=30.0):
    """Ждём фоновую синхронизацию до idle/error: импорт и повторный sync работают в отдельном потоке."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        links = client.get(f"/projects/{project_id}/jira", headers=_h(token)).json()["links"]
        if links and links[0]["status"] in ("idle", "error"):
            return links[0]
        time.sleep(0.1)
    raise AssertionError("синхронизация Jira не завершилась за отведённое время")


def test_e2e_import_sync_and_replace(client, jira_e2e, manager_token):
    """Полный цикл: connect → импорт (md + вложения на диске, всё в БД) → правка задачи в «Jira» → замена документа."""
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.job import Job
    from app.app.models.jira import JiraItem

    base = jira_e2e.base
    # ASCII-неверный токен: кириллица в токене умирала бы в httpx ещё до запроса (Jira-токены ASCII)
    r = client.post("/jira/connect", json={"token": "wrong-token"}, headers=_h(manager_token))
    assert r.status_code == 400 and "401" in r.json()["detail"]
    r = client.post("/jira/connect", json={"token": TOKEN}, headers=_h(manager_token))
    assert r.status_code == 200 and r.json() == {"connected": True, "name": "E2E \u0423\u0447\u0451\u0442\u043a\u0430", "projects": 1}
    listed = client.get("/jira/projects", headers=_h(manager_token)).json()["projects"]
    assert [(p["key"], p["linked"]) for p in listed] == [("E2E", None)]

    r = client.post("/jira/import", json={"projects": [{"key": "E2E"}]}, headers=_h(manager_token))
    assert r.status_code == 200, r.text
    project_id = r.json()["started"][0]["project_id"]
    link = _wait_link(client, manager_token, project_id)
    assert link["status"] == "idle", link
    assert link["status_text"] == "\u0437\u0430\u0434\u0430\u0447: \u043d\u043e\u0432\u044b\u0445 2, \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u043e 0, \u0431\u0435\u0437 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0439 0; "\
                                  "\u0444\u0430\u0439\u043b\u043e\u0432: \u043d\u043e\u0432\u044b\u0445 1, \u043f\u0440\u043e\u043f\u0443\u0449\u0435\u043d\u043e 1", link["status_text"]
    assert link["issues"] == 2 and link["files"] == 1, link

    names = sorted(a["original_filename"] for a in _artifacts(client, manager_token, project_id))
    assert names == ["Jira E2E-1 \u2014 \u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u043d\u043e\u0440\u043c\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u0438.md",
                     "Jira E2E-1 \u2014 \u043f\u043b\u0430\u043d.txt", "Jira E2E-2 \u2014 \u0412\u0442\u043e\u0440\u0430\u044f \u0437\u0430\u0434\u0430\u0447\u0430.md"], names
    assert all(a["status"] == "uploaded" and a["source_type"] == "document" for a in _artifacts(client, manager_token, project_id))

    doc_name = "Jira E2E-1 \u2014 \u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u043d\u043e\u0440\u043c\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u0438.md"
    file_name = "Jira E2E-1 \u2014 \u043f\u043b\u0430\u043d.txt"
    with SessionLocal() as db:
        doc = db.scalar(select(Artifact).where(Artifact.original_filename == doc_name))
        assert doc is not None and doc.status == "uploaded" and doc.source_type == "document"
        text = open(doc.file_path, encoding="utf-8").read()
        for expected in ("# [E2E-1] \u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u043d\u043e\u0440\u043c\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u0438",
                         f"{base}/browse/E2E-1", "\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 E2E: \u043d\u043e\u0440\u043c\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u044f \u043d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\u044b.",
                         "\u0421\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u0430\u0442\u044c \u0441 \u0437\u0430\u043a\u0430\u0437\u0447\u0438\u043a\u043e\u043c \u0434\u043e \u043f\u044f\u0442\u043d\u0438\u0446\u044b.",
                         "## \u0412\u043b\u043e\u0436\u0435\u043d\u0438\u044f", "- \u043f\u043b\u0430\u043d.txt (0 \u041a\u0411"):
            assert expected in text, f"\u0432 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u0435 \u043d\u0435\u0442: {expected!r}"
        file_art = db.scalar(select(Artifact).where(Artifact.original_filename == file_name))
        assert file_art is not None and open(file_art.file_path, "rb").read() == PLAN_BYTES
        assert db.scalar(select(Job).where(Job.artifact_id == doc.id)).kind == "process_artifact"  # дальше общий конвейер

        issues = {i.ref: i for i in db.scalars(select(JiraItem)).all() if i.kind == "issue"}
        assert set(issues) == {"E2E-1", "E2E-2"} and all(i.artifact_id is not None for i in issues.values())
        assert issues["E2E-1"].updated == "2026-11-10T09:30:00.000+0300"
        att = {i.ref: i for i in db.scalars(select(JiraItem)).all() if i.kind == "attachment"}
        assert set(att) == {"9001", "9002"}
        assert att["9001"].artifact_id is not None and att["9001"].issue_key == "E2E-1"
        assert att["9002"].artifact_id is None and att["9002"].note == "\u0432\u0438\u0434 \u0444\u0430\u0439\u043b\u0430 .epf \u043d\u0435 \u0440\u0430\u0437\u0431\u0438\u0440\u0430\u0435\u0442\u0441\u044f"
        old_doc_id, old_doc_path = doc.id, doc.file_path

    # «Заказчик» изменил задачу в Jira: повторная синхронизация заменяет документ (старый файл уходит с диска).
    jira_e2e.change("E2E-1", description="\u0418\u0437\u043c\u0435\u043d\u0438\u043b\u0438 \u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 E2E (\u043f\u043e\u0432\u0442\u043e\u0440\u043d\u0430\u044f \u0441\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0430\u0446\u0438\u044f).",
                    updated="2026-11-11T15:40:00.000+0300", add_comment="\u0412\u0442\u043e\u0440\u043e\u0439 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439 \u043f\u043e\u0441\u043b\u0435 \u043f\u0440\u0430\u0432\u043a\u0438.")
    assert client.post(f"/projects/{project_id}/jira/sync", headers=_h(manager_token)).status_code == 200
    link = _wait_link(client, manager_token, project_id)
    assert link["status"] == "idle", link
    assert link["status_text"] == "\u0437\u0430\u0434\u0430\u0447: \u043d\u043e\u0432\u044b\u0445 0, \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u043e 1, \u0431\u0435\u0437 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0439 1; "\
                                  "\u0444\u0430\u0439\u043b\u043e\u0432: \u043d\u043e\u0432\u044b\u0445 0, \u043f\u0440\u043e\u043f\u0443\u0449\u0435\u043d\u043e 0", link["status_text"]  # вложения уже зарегистрированы — дедупликация

    with SessionLocal() as db:
        new_doc = db.scalar(select(Artifact).where(Artifact.original_filename == doc_name))
        assert new_doc.id != old_doc_id, "\u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442 \u0437\u0430\u043c\u0435\u043d\u0438\u0442\u044b, \u0430 \u043d\u0435 \u0434\u043e\u043f\u043e\u043b\u043d\u0435\u043d"
        assert not os.path.exists(old_doc_path), "\u0441\u0442\u0430\u0440\u044b\u0439 \u0444\u0430\u0439\u043b \u0434\u043e\u043b\u0436\u0435\u043d \u0431\u044b\u0442\u044c \u0443\u0434\u0430\u043b\u0451\u043d"
        text = open(new_doc.file_path, encoding="utf-8").read()
        assert "\u0418\u0437\u043c\u0435\u043d\u0438\u043b\u0438 \u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 E2E (\u043f\u043e\u0432\u0442\u043e\u0440\u043d\u0430\u044f \u0441\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0430\u0446\u0438\u044f)." in text
        assert "\u0412\u0442\u043e\u0440\u043e\u0439 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439 \u043f\u043e\u0441\u043b\u0435 \u043f\u0440\u0430\u0432\u043a\u0438." in text and "## \u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0438 (2)" in text
        item = db.scalar(select(JiraItem).where(JiraItem.kind == "issue", JiraItem.ref == "E2E-1"))
        assert item.updated == "2026-11-11T15:40:00.000+0300"


def test_e2e_scheduled_pass_rechecks_and_updates(client, jira_e2e, manager_token):
    """Расписной проход по настоящему HTTP-клиенту: перепроверка видимости токена и докачка изменений."""
    from app.app.config import get_settings

    client.post("/jira/connect", json={"token": TOKEN}, headers=_h(manager_token))
    project_id = client.post("/jira/import", json={"projects": [{"key": "E2E"}]}, headers=_h(manager_token)).json()["started"][0]["project_id"]
    assert _wait_link(client, manager_token, project_id)["status"] == "idle"

    jira_e2e.change("E2E-2", description="\u0418\u0437\u043c\u0435\u043d\u0438\u043b\u0438 \u0432\u0442\u043e\u0440\u0443\u044e \u0437\u0430\u0434\u0430\u0447\u0443.", updated="2026-11-11T18:00:00.000+0300")
    assert ji.scheduled_pass(get_settings()) == {"accounts": 1, "links": 1}
    state = client.get(f"/projects/{project_id}/jira", headers=_h(manager_token)).json()["links"][0]
    assert state["status"] == "idle" and "\u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u043e 1" in state["status_text"], state
