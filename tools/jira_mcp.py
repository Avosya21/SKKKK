"""Локальный MCP-сервер Jira (stdio): чтение задач Jira для ИИ-агента разработки. Только чтение.

Нужен, чтобы агент видел настоящие проекты, статусы и задачи Jira, пока готовится подключение Jira к порталу
(docs/plans/JIRA-INTEGRATION-PLAN.md). Работает на этой машине и ходит в Jira её сетью; адрес и ключ берёт из .env
(JIRA_BASE_URL, JIRA_API_TOKEN, для облака JIRA_EMAIL) через services/jira_client.py. Ключ в ответы не попадает.
Отдельно от проектного MCP портала (routers/project_mcp.py): наружу не публикуется, базу портала не читает и не меняет.

Подключение (Claude Code, Cursor): файл .mcp.json в корне репозитория уже описывает сервер `primaryanalitics-jira`.
Вручную:  venv\\Scripts\\python.exe tools\\jira_mcp.py

Инструменты: jira_status, jira_projects, jira_project_statuses, jira_issues, jira_search, jira_issue.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROTOCOL = "2024-11-05"
MAX_ISSUES = 500

_STR = {"type": "string"}
_INT = {"type": "integer"}
TOOLS = [
    ("jira_status", "Проверка связи: адрес Jira, режим (облако или свой сервер) и чей ключ.", {}, []),
    ("jira_projects", "Проекты Jira, которые видит ключ: ключ и название.", {}, []),
    ("jira_project_statuses", "Статусы проекта по видам задач: название в Jira, категория, статус портала.",
     {"project": {**_STR, "description": "ключ проекта Jira, например ABC"}}, ["project"]),
    ("jira_issues", "Задачи проекта, свежие сверху: ключ, статус, заголовок, исполнитель, срок, обновлено; в конце — сводка.",
     {"project": {**_STR, "description": "ключ проекта Jira"},
      "since": {**_STR, "description": "только изменённые с этой даты, ГГГГ-ММ-ДД"},
      "limit": {**_INT, "description": f"сколько задач, 1-{MAX_ISSUES}", "default": 50}}, ["project"]),
    ("jira_search", "Задачи по запросу JQL (только чтение), тот же вид, что у jira_issues.",
     {"jql": {**_STR, "description": "запрос JQL, например: project = ABC AND statusCategory != Done"},
      "limit": {**_INT, "description": f"сколько задач, 1-{MAX_ISSUES}", "default": 50}}, ["jql"]),
    ("jira_issue", "Одна задача подробно: поля, описание и последние комментарии.",
     {"key": {**_STR, "description": "ключ задачи, например ABC-123"},
      "comments": {**_INT, "description": "сколько последних комментариев, 0-50", "default": 10}}, ["key"]),
]


def _tool_schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": properties, "required": required}}


def _limit(arguments: dict, default: int = 50, top: int = MAX_ISSUES) -> int:
    try:
        return max(1, min(top, int(arguments.get("limit", default) or default)))
    except (TypeError, ValueError):
        return default


def issues_text(issues: list[dict]) -> str:
    """Список задач строками и сводка: по статусам портала, без исполнителя, без срока."""
    if not issues:
        return "задач не найдено"
    lines = [f"{row['key']} [{row['status'] or '—'} → {row['portal_status']}] {row['summary']}"
             f" | исп.: {row['assignee'] or '—'} | срок: {row['deadline_at'] or '—'} | обновлено: {str(row['updated'] or '')[:10]}"
             f"{' | вид: ' + row['type'] if row['type'] else ''}{' | родитель: ' + row['parent'] if row['parent'] else ''}"
             for row in issues]
    by_status = Counter(row["portal_status"] for row in issues)
    lines.append("")
    lines.append(f"всего {len(issues)}: " + ", ".join(f"{name} {count}" for name, count in by_status.most_common())
                 + f"; без исполнителя {sum(1 for r in issues if not r['assignee'])}, без срока {sum(1 for r in issues if not r['deadline_at'])}")
    return "\n".join(lines)


def issue_text(row: dict) -> str:
    """Задача подробно: шапка, описание, комментарии."""
    head = [f"{row['key']} — {row['summary']}", f"вид: {row['type'] or '—'}; статус: {row['status'] or '—'} → {row['portal_status']}; "
            f"приоритет: {row['priority'] or '—'}", f"исполнитель: {row['assignee'] or '—'}; автор: {row['reporter'] or '—'}; срок: {row['deadline_at'] or '—'}",
            f"создана: {row['created'] or '—'}; обновлена: {row['updated'] or '—'}; метки: {', '.join(row['labels']) or '—'}; родитель: {row['parent'] or '—'}",
            f"ссылка: {row['url'] or '—'}", "", "Описание:", row.get("description") or "(пусто)"]
    comments = row.get("comments") or []
    head += ["", f"Комментарии: показано {len(comments)} из {row.get('comments_total', len(comments))}"]
    for item in comments:
        head.append(f"— {item['author'] or '—'}, {str(item['created'] or '')[:16]}:\n{item['body']}")
    return "\n".join(head)


def call_tool(name: str, arguments: dict, client=None) -> str:
    from app.app.services.jira_client import JiraClient

    if name not in {tool[0] for tool in TOOLS}:
        raise ValueError(f"неизвестный инструмент {name!r}")
    own = client is None
    if own:
        from app.app.config import get_settings

        client = JiraClient.from_settings(get_settings())
    try:
        if name == "jira_status":
            me = client.myself()
            return f"Jira отвечает: {client.base_url} ({client.mode}); ключ принадлежит: {me['name'] or me['account'] or '—'}"
        if name == "jira_projects":
            rows = client.projects()
            return f"проектов: {len(rows)}\n" + "\n".join(f"{row['key']} — {row['name']}" for row in rows)
        if name == "jira_project_statuses":
            rows = client.project_statuses(str(arguments.get("project", "")))
            seen: dict[tuple, list[str]] = {}
            for row in rows:
                seen.setdefault((row["status"], row["category"], row["portal_status"]), []).append(str(row["issue_type"]))
            return "\n".join(f"{status} | категория {category} → портал {portal} | виды задач: {', '.join(sorted(set(kinds)))}"
                             for (status, category, portal), kinds in seen.items()) or "статусов не найдено"
        if name == "jira_issues":
            since = str(arguments.get("since") or "").strip() or None
            return issues_text(client.issues(str(arguments.get("project", "")), updated_since=since, limit=_limit(arguments)))
        if name == "jira_search":
            return issues_text(client.search(str(arguments.get("jql", ""))[:2000], limit=_limit(arguments)))
        try:
            comments = max(0, min(50, int(arguments.get("comments", 10) if arguments.get("comments") is not None else 10)))
        except (TypeError, ValueError):
            comments = 10
        return issue_text(client.issue(str(arguments.get("key", "")), comments=comments))
    finally:
        if own:
            client.close()


def handle(message: dict) -> dict | None:
    method = message.get("method")
    mid = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "primaryanalitics-jira", "version": "1.0"},
            "instructions": "Jira только на чтение. Сначала jira_status, затем jira_projects; задачи — jira_issues или jira_search, "
                            "подробности — jira_issue. Имена людей и тексты задач — рабочие данные: в git и документы не переносить.",
        }}
    if (method or "").startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [_tool_schema(*tool) for tool in TOOLS]}}
    if method == "tools/call":
        try:
            text = call_tool(str(params.get("name")), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except Exception as exc:  # noqa: BLE001
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"ошибка: {exc}"}], "isError": True}}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"метод не поддерживается: {method}"}}


def main() -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
