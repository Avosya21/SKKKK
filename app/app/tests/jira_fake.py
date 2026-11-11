"""Подставная Jira для тестов: проекты, задачи со всеми видами данных, вложения. Отвечает и через httpx.MockTransport, и по настоящему сокету."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx

from app.app.services import jira_client as jc

AUTHOR = {"displayName": "Тестовый Автор", "name": "author"}
WORKER = {"displayName": "Тестовый Исполнитель", "name": "worker"}


def issue(key: str, summary: str, updated: str, *, category: str = "indeterminate", status: str = "В работе", attachments=None,
          comments: int = 1, worklogs: int = 0, rich: bool = False) -> dict:
    """Задача в том виде, как её отдаёт Jira с ``fields=*all&expand=names,changelog``."""
    fields = {
        "summary": summary, "issuetype": {"name": "Задача"}, "priority": {"name": "Средний"},
        "status": {"name": status, "statusCategory": {"key": category}}, "assignee": dict(WORKER), "reporter": dict(AUTHOR),
        "creator": dict(AUTHOR), "duedate": None, "created": "2026-10-01T10:00:00.000+0300", "updated": updated, "labels": [],
        "project": {"key": key.split("-")[0], "name": "Проект"}, "description": f"Описание задачи {key}.\r\nВторая строка [^схема.txt].",
        "comment": {"total": comments, "comments": [{"id": str(n), "author": dict(AUTHOR), "created": f"2026-10-{n + 1:02d}T10:00:00.000+0300",
                                                    "updated": f"2026-10-{n + 1:02d}T10:00:00.000+0300",
                                                    "body": f"Комментарий {n + 1}: договорились сделать до конца месяца."} for n in range(comments)]},
        "worklog": {"total": worklogs, "worklogs": [{"id": str(n), "author": dict(WORKER), "started": f"2026-10-{n + 1:02d}T09:00:00.000+0300",
                                                    "timeSpent": "1h", "timeSpentSeconds": 3600, "comment": f"работа {n + 1}"} for n in range(worklogs)]},
        "attachment": attachments or [], "issuelinks": [], "subtasks": [], "components": [], "fixVersions": [], "versions": [],
        "resolution": None, "resolutiondate": None, "environment": None, "timetracking": {}, "watches": {"watchCount": 0},
        "customfield_10001": None, "timespent": None, "workratio": -1,
    }
    raw = {"key": key, "fields": fields, "names": {"customfield_10001": "Спринт", "customfield_10002": "Оценка в баллах",
                                                  "customfield_10003": "Заказчик", "customfield_10004": "Rank"},
           "changelog": {"total": 0, "histories": []}}
    if rich:
        long_text = "Очень длинный комментарий. " + "Слово " * 6000 + "КОНЕЦ-КОММЕНТАРИЯ"
        fields["comment"]["comments"].append({"id": "900", "author": dict(WORKER), "created": "2026-10-20T10:00:00.000+0300",
                                             "updated": "2026-10-21T11:00:00.000+0300", "body": long_text})
        fields["comment"]["total"] = len(fields["comment"]["comments"])
        fields.update({
            "resolution": {"name": "Готово"}, "resolutiondate": "2026-11-01T12:00:00.000+0300", "duedate": "2026-11-15",
            "labels": ["обмен", "срочно"], "components": [{"name": "Обмен с сайтом"}], "fixVersions": [{"name": "Релиз 2"}],
            "versions": [{"name": "Релиз 1"}], "environment": "Тестовый контур",
            "timetracking": {"originalEstimate": "2d", "remainingEstimate": "4h", "timeSpent": "1d 4h"},
            "watches": {"watchCount": 3}, "parent": {"key": "ABC-100", "fields": {"summary": "Эпик обмена"}},
            "subtasks": [{"key": "ABC-11", "fields": {"summary": "Подзадача раз", "status": {"name": "Готово"}}}],
            "issuelinks": [{"type": {"outward": "блокирует", "inward": "блокируется"},
                            "outwardIssue": {"key": "ABC-12", "fields": {"summary": "Зависимая", "status": {"name": "Новая"}}}}],
            "customfield_10001": ["com.atlassian.greenhopper.service.sprint.Sprint@1f[id=5,rapidViewId=2,state=ACTIVE,name=Спринт 7,goal=]"],
            "customfield_10002": 5.0, "customfield_10003": {"value": "Заказчик А", "child": {"value": "Отдел закупок"}},
            "customfield_10004": "0|i0001z:",
        })
        raw["changelog"] = {"total": 2, "histories": [
            {"created": "2026-10-05T10:00:00.000+0300", "author": dict(AUTHOR),
             "items": [{"field": "status", "fromString": "Новая", "toString": "В работе"}, {"field": "assignee", "fromString": None, "toString": "Тестовый Исполнитель"}]},
            {"created": "2026-10-06T10:00:00.000+0300", "author": dict(WORKER), "items": [{"field": "description", "fromString": "старый текст", "toString": "новый текст"}]}]}
    return raw


class FakeJira:
    """Данные и ответы подставной Jira. ``visible`` — какие проекты видит какой токен."""

    PAGE = 2  # сколько комментариев и трудозатрат Jira «вкладывает» в задачу; остальное — постранично

    def __init__(self, base: str = "https://jira.example"):
        self.base = base
        self.visible = {"tok-manager": ["ABC", "XYZ"], "tok-user": ["ABC"], "tok-admin": ["ABC", "XYZ", "OLD"]}
        self.names = {"ABC": "Проект Альфа", "XYZ": "Проект Икс", "OLD": "Старый проект"}
        self.files = {"501": b"plain text attachment \xd1\x82\xd0\xb5\xd0\xba\xd1\x81\xd1\x82", "502": b"zip-bytes", "503": b"mp3"}
        self.fail_download: set[str] = set()
        self.issues: dict[str, dict[str, dict]] = {"ABC": {}, "XYZ": {}, "OLD": {}}
        self.put(issue("ABC-1", "Настроить обмен", "2026-11-01T10:00:00.000+0300", rich=True, comments=5, worklogs=4, attachments=[
            self.attachment("501", "схема.txt"), self.attachment("502", "дамп.zip")]))
        self.put(issue("ABC-2", "Сверить остатки", "2026-11-02T10:00:00.000+0300"))
        self.put(issue("XYZ-1", "Первая задача", "2026-11-03T10:00:00.000+0300"))
        self.detail_calls: list[str] = []

    def attachment(self, ref: str, filename: str) -> dict:
        return {"id": ref, "filename": filename, "size": len(self.files[ref]), "created": "2026-11-01T09:00:00.000+0300",
                "author": dict(WORKER), "content": f"{self.base}/secure/attachment/{ref}/file"}

    def put(self, raw: dict) -> None:
        self.issues[raw["key"].split("-")[0]][raw["key"]] = raw

    def respond(self, method: str, path: str, params: dict[str, str], token: str) -> tuple[int, object]:
        keys = self.visible.get(token)
        if keys is None:
            return 401, {}
        if path.endswith("/myself"):
            return 200, {"displayName": f"Учётка {token.split('-')[-1]}"}
        if path.endswith("/rest/api/2/project"):
            return 200, [{"key": k, "name": self.names[k]} for k in keys]
        if "/rest/api/2/project/" in path:
            key = path.rsplit("/", 1)[1]
            if key not in keys:
                return 404, {}
            return 200, {"key": key, "name": self.names[key], "description": "Описание проекта.", "lead": dict(AUTHOR), "projectTypeKey": "software",
                         "components": [{"name": "Обмен с сайтом"}], "versions": [{"name": "Релиз 1", "released": True, "releaseDate": "2026-09-01"}]}
        if path.endswith("/search"):
            key = params["jql"].split('"')[1]
            if key not in keys:
                return 400, {}
            rows = sorted(self.issues[key].values(), key=lambda r: r["fields"]["updated"], reverse=True)
            start, size = int(params.get("startAt", 0)), int(params.get("maxResults", 50))
            return 200, {"issues": rows[start:start + size], "total": len(rows)}
        if "/secure/attachment/" in path:
            ref = path.split("/secure/attachment/")[1].split("/")[0]
            return (500, b"") if ref in self.fail_download else (200, self.files[ref])
        if "/issue/" in path:
            tail = path.split("/issue/")[1].split("/")
            key = tail[0]
            raw = self.issues.get(key.split("-")[0], {}).get(key)
            if raw is None or key.split("-")[0] not in keys:
                return 404, {}
            if len(tail) == 1:
                self.detail_calls.append(key)
                out = json.loads(json.dumps(raw))
                for name, list_key in (("comment", "comments"), ("worklog", "worklogs")):  # как настоящая Jira: в задаче только начало списка
                    out["fields"][name][list_key] = out["fields"][name][list_key][: self.PAGE]
                return 200, out
            if tail[1] == "remotelink":
                return 200, [{"object": {"title": "Инструкция", "url": "https://docs.example/instr"}}] if key == "ABC-1" else []
            list_key = {"comment": "comments", "worklog": "worklogs"}[tail[1]]
            items = raw["fields"][tail[1]][list_key]
            if tail[1] == "worklog":  # как Jira на своём сервере: startAt не слушает, отдаёт все записи сразу
                return 200, {list_key: items, "total": len(items)}
            start, size = int(params.get("startAt", 0)), int(params.get("maxResults", 50))
            return 200, {list_key: items[start:start + size], "total": len(items)}
        return 404, {}

    # --- через httpx.MockTransport ---
    def handler(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        status, body = self.respond(request.method, request.url.path, dict(request.url.params), token)
        return httpx.Response(status, content=body) if isinstance(body, bytes) else httpx.Response(status, json=body)

    def factory(self, settings, token):
        return jc.JiraClient(self.base, token, transport=httpx.MockTransport(self.handler))

    # --- по настоящему сокету ---
    def serve(self) -> ThreadingHTTPServer:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                status, body = fake.respond("GET", url.path, {k: v[0] for k, v in parse_qs(url.query).items()}, token)
                data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream" if isinstance(body, bytes) else "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        for project in self.issues.values():
            for raw in project.values():
                for a in raw["fields"]["attachment"]:
                    a["content"] = f"{self.base}/secure/attachment/{a['id']}/file"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
