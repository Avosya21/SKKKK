"""Клиент Jira (только чтение): проверка связи, список проектов, задачи проекта — docs/plans/JIRA-INTEGRATION-PLAN.md, этап 1.

Работает и с облачной Jira, и с Jira на своём сервере (Data Center / Server) — режим выбирается по настройкам:
``JIRA_EMAIL`` задан — облако (Basic: почта + API-токен, поиск ``/rest/api/2/search/jql`` с ``nextPageToken``);
``JIRA_EMAIL`` пуст — свой сервер (Bearer: персональный токен доступа, поиск ``/rest/api/2/search`` со ``startAt``).
В Jira ничего не пишет. Сеть — только здесь; разбор ответа (``normalize_issue``) — чистая функция без сети.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

from app.app.config import Settings

logger = logging.getLogger("primaryanalitics.jira")

ISSUE_FIELDS = ("summary", "status", "assignee", "reporter", "duedate", "created", "updated", "issuetype", "priority", "labels", "parent")
PAGE_SIZE = 100
TIMEOUT_SEC = 30
# Категория статуса Jira -> статус задачи портала (task_board.STATUSES); «отменена» в Jira отдельной категории не имеет.
STATUS_BY_CATEGORY = {"new": "open", "indeterminate": "in_progress", "done": "done"}


class JiraError(RuntimeError):
    """Ошибка обращения к Jira с понятным человеку текстом."""


class JiraNotConfigured(JiraError):
    """В .env не заданы адрес Jira или токен."""


def _name(value: Any, *keys: str) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def portal_status(status: Any) -> str:
    """Статус задачи портала по статусу Jira: по категории (новая / в работе / готово), неизвестное — ``open``."""
    category = status.get("statusCategory") if isinstance(status, dict) else None
    key = category.get("key") if isinstance(category, dict) else None
    return STATUS_BY_CATEGORY.get(str(key or "").lower(), "open")


def normalize_issue(raw: dict[str, Any], base_url: str = "") -> dict[str, Any]:
    """Задача Jira в плоском виде: ключ, заголовок, статус (как в Jira и как в портале), исполнитель, срок, даты, ссылка."""
    fields = raw.get("fields") or {}
    key = str(raw.get("key") or "")
    parent = fields.get("parent") if isinstance(fields.get("parent"), dict) else {}
    return {
        "key": key,
        "id": str(raw.get("id")).strip() if raw.get("id") not in (None, "") else None,
        "summary": str(fields.get("summary") or "").strip(),
        "type": _name(fields.get("issuetype"), "name"),
        "status": _name(fields.get("status"), "name"),
        "portal_status": portal_status(fields.get("status")),
        "assignee": _name(fields.get("assignee"), "displayName", "name"),
        "reporter": _name(fields.get("reporter"), "displayName", "name"),
        "priority": _name(fields.get("priority"), "name"),
        "deadline_at": (str(fields.get("duedate") or "")[:10] or None),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "labels": [str(x) for x in fields.get("labels") or []],
        "parent": parent.get("key"),
        "url": f"{base_url.rstrip('/')}/browse/{key}" if base_url and key else None,
    }


class JiraClient:
    """Чтение из Jira. ``email`` задан — облако, пуст — свой сервер (см. докстринг модуля)."""

    def __init__(self, base_url: str, token: str, *, email: str = "", verify_tls: bool = True,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.email = (email or "").strip()
        token = (token or "").strip()
        if not self.base_url or not token:
            raise JiraNotConfigured("Jira не настроена: задайте JIRA_BASE_URL и JIRA_API_TOKEN в .env")
        if not self.base_url.lower().startswith(("http://", "https://")):
            raise JiraNotConfigured("JIRA_BASE_URL должен начинаться с https:// (или http:// для внутреннего сервера)")
        try:
            token.encode("ascii")
            self.email.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise JiraNotConfigured("Токен Jira содержит недопустимые символы: скопируйте его из Jira заново, без пробелов и русских букв") from exc
        self.cloud = bool(self.email)
        auth = httpx.BasicAuth(self.email, token) if self.cloud else None
        headers = {"Accept": "application/json"}
        if not self.cloud:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(base_url=self.base_url, auth=auth, headers=headers, timeout=TIMEOUT_SEC,
                                  verify=verify_tls, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> "JiraClient":
        return cls(settings.jira_base_url, settings.jira_api_token, email=settings.jira_email,
                   verify_tls=settings.jira_verify_tls, transport=transport)

    @property
    def mode(self) -> str:
        return "облако" if self.cloud else "свой сервер"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "JiraClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise JiraError(f"Jira не ответила за {TIMEOUT_SEC} с: {self.base_url}") from exc
        except httpx.HTTPError as exc:
            raise JiraError(f"Нет связи с Jira ({self.base_url}): {exc.__class__.__name__}: {exc}") from exc
        if response.status_code == 401:
            raise JiraError("Jira не приняла ключ (401): проверьте JIRA_API_TOKEN"
                            + (" и JIRA_EMAIL" if self.cloud else "; для облачной Jira нужен ещё JIRA_EMAIL"))
        if response.status_code == 403:
            raise JiraError("Jira отказала в доступе (403): у учётной записи ключа нет прав на этот раздел")
        if response.status_code == 404:
            raise JiraError(f"Jira не нашла {path} (404): проверьте JIRA_BASE_URL и ключ проекта")
        if response.status_code == 429:
            raise JiraError("Jira ограничила частоту запросов (429): повторите позже")
        if response.status_code >= 400:
            raise JiraError(f"Jira вернула ошибку {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise JiraError(f"Jira ответила не JSON ({path}): по адресу JIRA_BASE_URL, похоже, не Jira или страница входа") from exc

    def myself(self) -> dict[str, Any]:
        """Чьим ключом работаем: имя и учётная запись. Первая проверка связи."""
        data = self._request("GET", "/rest/api/2/myself")
        return {"name": _name(data, "displayName", "name"), "account": _name(data, "emailAddress", "name", "accountId")}

    def projects(self) -> list[dict[str, Any]]:
        """Проекты, которые видит учётная запись ключа: ключ, название, вид."""
        data = self._request("GET", "/rest/api/2/project")
        rows = [{"key": str(p.get("key") or ""), "name": str(p.get("name") or ""), "type": p.get("projectTypeKey")}
                for p in data if isinstance(p, dict)]
        return sorted(rows, key=lambda row: row["key"])

    def _jql(self, project_key: str, updated_since: str | None) -> str:
        key = project_key.strip().upper()
        if not key or not all(ch.isalnum() or ch == "_" for ch in key):
            raise JiraError(f"Неверный ключ проекта Jira: {project_key!r} (ожидается, например, ABC)")
        jql = f'project = "{key}"'
        if updated_since:
            day = str(updated_since)[:10]
            if len(day) != 10 or not day.replace("-", "").isdigit():
                raise JiraError(f"Дата для отбора — ГГГГ-ММ-ДД, получено: {updated_since!r}")
            jql += f' AND updated >= "{day}"'
        return jql + " ORDER BY updated DESC"

    def iter_issues(self, project_key: str, *, updated_since: str | None = None, limit: int | None = None) -> Iterator[dict[str, Any]]:
        """Задачи проекта, свежие сверху. ``updated_since`` — ГГГГ-ММ-ДД, ``limit`` — не больше стольких задач."""
        return self.iter_search(self._jql(project_key, updated_since), limit=limit)

    def iter_search(self, jql: str, *, limit: int | None = None) -> Iterator[dict[str, Any]]:
        """Задачи по запросу JQL (только чтение), постранично."""
        jql = (jql or "").strip()
        if not jql:
            raise JiraError("Пустой запрос JQL")
        fields = ",".join(ISSUE_FIELDS)
        given = 0
        start, token = 0, None
        while True:
            size = PAGE_SIZE if limit is None else max(1, min(PAGE_SIZE, limit - given))
            if self.cloud:
                params: dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": size}
                if token:
                    params["nextPageToken"] = token
                data = self._request("GET", "/rest/api/2/search/jql", params=params)
            else:
                data = self._request("GET", "/rest/api/2/search", params={"jql": jql, "fields": fields, "maxResults": size, "startAt": start})
            issues = data.get("issues") or []
            for raw in issues:
                yield normalize_issue(raw, self.base_url)
                given += 1
                if limit is not None and given >= limit:
                    return
            if not issues:
                return
            if self.cloud:
                token = data.get("nextPageToken")
                if not token or data.get("isLast"):
                    return
            else:
                start += len(issues)
                if start >= int(data.get("total") or 0):
                    return

    def issues(self, project_key: str, *, updated_since: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        return list(self.iter_issues(project_key, updated_since=updated_since, limit=limit))

    def search(self, jql: str, *, limit: int | None = 50) -> list[dict[str, Any]]:
        return list(self.iter_search(jql, limit=limit))

    def issue(self, key: str, *, comments: int = 20) -> dict[str, Any]:
        """Одна задача подробно: поля списка, описание и последние комментарии (автор, дата, текст)."""
        key = (key or "").strip().upper()
        if not key or not all(ch.isalnum() or ch in "-_" for ch in key):
            raise JiraError(f"Неверный ключ задачи Jira: {key!r} (ожидается, например, ABC-123)")
        raw = self._request("GET", f"/rest/api/2/issue/{key}", params={"fields": ",".join(ISSUE_FIELDS + ("description", "comment"))})
        fields = raw.get("fields") or {}
        row = normalize_issue(raw, self.base_url)
        description = fields.get("description")
        row["description"] = description if isinstance(description, str) else (str(description) if description else "")
        block = fields.get("comment") if isinstance(fields.get("comment"), dict) else {}
        items = [c for c in block.get("comments") or [] if isinstance(c, dict)]
        row["comments_total"] = int(block.get("total") or len(items))
        row["comments"] = [{"author": _name(c.get("author"), "displayName", "name"), "created": c.get("created"),
                            "body": c.get("body") if isinstance(c.get("body"), str) else str(c.get("body") or "")}
                           for c in items[-max(0, comments):]] if comments > 0 else []
        return row

    def issue_raw(self, key: str) -> dict[str, Any]:
        """Задача целиком, как отдаёт Jira: поля списка, описание, комментарии, вложения, связи — для загрузки в портал."""
        key = (key or "").strip().upper()
        if not key or not all(ch.isalnum() or ch in "-_" for ch in key):
            raise JiraError(f"Неверный ключ задачи Jira: {key!r} (ожидается, например, ABC-123)")
        fields = ",".join(ISSUE_FIELDS + ("description", "comment", "attachment", "issuelinks", "project"))
        return self._request("GET", f"/rest/api/2/issue/{key}", params={"fields": fields})

    def _paged(self, path: str, key: str, have: list, total: int) -> list:
        """Дочитать список (комментарии, трудозатраты), если Jira отдала в задаче только его начало.

        Список запрашивается заново с начала и собирается по ``id`` без повторов: часть версий Jira не слушает ``startAt``
        и отдаёт всё сразу — тогда хватает одного запроса, а записи не задваиваются.
        """
        seen: dict[str, dict] = {}
        start = 0
        while len(seen) < total:
            data = self._request("GET", path, params={"startAt": start, "maxResults": 100})
            page = [x for x in data.get(key) or [] if isinstance(x, dict)]
            before = len(seen)
            for position, item in enumerate(page):
                seen.setdefault(str(item.get("id") or f"{start}:{position}"), item)
            if not page or len(seen) == before:
                break
            start += len(page)
        return list(seen.values()) if len(seen) >= len(have) else list(have)

    def issue_full(self, key: str) -> dict[str, Any]:
        """Задача со всем, что есть в Jira: все поля (включая пользовательские) с их названиями (``names``), история
        изменений (``changelog``), все комментарии и все записи трудозатрат (дочитываются постранично), внешние ссылки.
        """
        key = (key or "").strip().upper()
        if not key or not all(ch.isalnum() or ch in "-_" for ch in key):
            raise JiraError(f"Неверный ключ задачи Jira: {key!r} (ожидается, например, ABC-123)")
        raw = self._request("GET", f"/rest/api/2/issue/{key}", params={"fields": "*all", "expand": "names,changelog"})
        fields = raw.setdefault("fields", {})
        for name, path, list_key in (("comment", f"/rest/api/2/issue/{key}/comment", "comments"),
                                     ("worklog", f"/rest/api/2/issue/{key}/worklog", "worklogs")):
            block = fields.get(name) if isinstance(fields.get(name), dict) else None
            if block is None:
                continue
            have = [x for x in block.get(list_key) or [] if isinstance(x, dict)]
            total = int(block.get("total") or len(have))
            if total > len(have):
                block[list_key] = self._paged(path, list_key, have, total)
        log = raw.get("changelog") if isinstance(raw.get("changelog"), dict) else {}
        histories = [h for h in log.get("histories") or [] if isinstance(h, dict)]
        raw["changelog_complete"] = int(log.get("total") or len(histories)) <= len(histories)
        try:
            links = self._request("GET", f"/rest/api/2/issue/{key}/remotelink")
            raw["remotelinks"] = [x for x in links if isinstance(x, dict)] if isinstance(links, list) else []
        except JiraError:
            raw["remotelinks"] = []  # внешние ссылки могут быть закрыты правами — задача от этого не теряется
        try:
            watchers = self._request("GET", f"/rest/api/2/issue/{key}/watchers")
            raw["watchers"] = [x for x in (watchers.get("watchers") or []) if isinstance(x, dict)] if isinstance(watchers, dict) else []
        except JiraError:
            raw["watchers"] = []  # список наблюдателей виден не всем — остаётся их число из самой задачи
        return raw

    def project_info(self, project_key: str) -> dict[str, Any]:
        """Сведения о проекте Jira: название, описание, руководитель, компоненты, версии."""
        key = project_key.strip().upper()
        if not key or not all(ch.isalnum() or ch == "_" for ch in key):
            raise JiraError(f"Неверный ключ проекта Jira: {project_key!r} (ожидается, например, ABC)")
        return self._request("GET", f"/rest/api/2/project/{key}")

    def project_card(self, project_key: str) -> dict[str, Any]:
        """Карточка проекта для сводки: ответ Jira и состав ролей. Ошибка одной роли не отменяет остальные сведения."""
        info = dict(self.project_info(project_key))
        info["roles_resolved"] = self._project_roles(info.get("roles"))
        return info

    def _same_origin_path(self, url: Any) -> str | None:
        """Путь запроса только на эту же Jira. Чужой адрес не запрашиваем."""
        text = str(url or "").strip()
        if not text:
            return None
        if text.startswith("/") and not text.startswith("//"):
            return text
        base, parsed = urlparse(self.base_url), urlparse(text)
        if parsed.scheme in {"http", "https"} and parsed.netloc == base.netloc and parsed.path.startswith("/"):
            return parsed.path + (f"?{parsed.query}" if parsed.query else "")
        return None

    def _project_roles(self, roles: Any) -> list[dict[str, Any]]:
        if not isinstance(roles, dict):
            return []
        out: list[dict[str, Any]] = []
        for name, url in roles.items():
            row: dict[str, Any] = {"name": str(name), "id": None, "actors": [], "missing": True}
            path = self._same_origin_path(url)
            data = None
            if path:
                try:
                    data = self._request("GET", path)
                except JiraError:
                    data = None
            if isinstance(data, dict):
                row["missing"] = False
                row["name"] = str(data.get("name") or name)
                row["id"] = data.get("id")
                for actor in data.get("actors") or []:
                    if not isinstance(actor, dict):
                        continue
                    user = actor.get("actorUser") if isinstance(actor.get("actorUser"), dict) else {}
                    group = actor.get("actorGroup") if isinstance(actor.get("actorGroup"), dict) else {}
                    who = str(actor.get("displayName") or _name(user, "displayName", "name") or _name(group, "displayName", "name") or "").strip()
                    account = str(user.get("name") or user.get("accountId") or group.get("name") or "").strip()
                    kind = "группа" if group or "group" in str(actor.get("type") or "").lower() else "пользователь"
                    if who or account:
                        row["actors"].append({"name": who or account, "kind": kind, "account": account})
            out.append(row)
        return sorted(out, key=lambda item: str(item["name"]).lower())

    def count(self, project_key: str) -> int:
        """Сколько задач в проекте Jira."""
        jql = self._jql(project_key, None)
        if self.cloud:
            data = self._request("POST", "/rest/api/2/search/approximate-count", json={"jql": jql.split(" ORDER BY")[0]})
            return int(data.get("count") or 0)
        data = self._request("GET", "/rest/api/2/search", params={"jql": jql, "fields": "key", "maxResults": 0})
        return int(data.get("total") or 0)

    def download(self, url: str, dest, *, max_bytes: int) -> int:
        """Скачать вложение Jira в файл ``dest``; больше ``max_bytes`` — ошибка, файл удаляется. Только адреса этой Jira."""
        if not str(url).startswith(self.base_url + "/"):
            raise JiraError("Адрес вложения не принадлежит этой Jira")
        size = 0
        try:
            with self._http.stream("GET", url, follow_redirects=True) as response:
                if response.status_code >= 400:
                    raise JiraError(f"Jira не отдала вложение ({response.status_code})")
                with open(dest, "wb") as out:
                    for chunk in response.iter_bytes(1024 * 256):
                        size += len(chunk)
                        if size > max_bytes:
                            raise JiraError("Вложение больше допустимого размера")
                        out.write(chunk)
        except (httpx.HTTPError, JiraError) as exc:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(exc, JiraError):
                raise
            raise JiraError(f"Вложение не скачалось: {exc.__class__.__name__}") from exc
        return size

    def project_statuses(self, project_key: str) -> list[dict[str, Any]]:
        """Статусы проекта по видам задач: название в Jira, категория и статус портала — для сопоставления."""
        key = project_key.strip().upper()
        if not key or not all(ch.isalnum() or ch == "_" for ch in key):
            raise JiraError(f"Неверный ключ проекта Jira: {project_key!r} (ожидается, например, ABC)")
        data = self._request("GET", f"/rest/api/2/project/{key}/statuses")
        rows: list[dict[str, Any]] = []
        for kind in data if isinstance(data, list) else []:
            for status in kind.get("statuses") or []:
                category = status.get("statusCategory") if isinstance(status.get("statusCategory"), dict) else {}
                rows.append({"issue_type": kind.get("name"), "status": status.get("name"), "category": category.get("key"),
                             "portal_status": portal_status(status)})
        return rows


def configured(settings: Settings) -> bool:
    """Заданы ли адрес и ключ Jira."""
    return bool((settings.jira_base_url or "").strip() and (settings.jira_api_token or "").strip())


def project_keys(settings: Settings) -> list[str]:
    """Ключи проектов Jira из ``JIRA_PROJECT_KEYS`` (через запятую), без повторов, в верхнем регистре."""
    seen: list[str] = []
    for part in (settings.jira_project_keys or "").replace(";", ",").split(","):
        key = part.strip().upper()
        if key and key not in seen:
            seen.append(key)
    return seen
