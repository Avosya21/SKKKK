"""Первые данные из Jira: проверка связи, список проектов и выгрузка задач проекта (только чтение).

  venv\\Scripts\\python.exe scripts\\jira_pull.py                    # проверить ключ и показать доступные проекты
  venv\\Scripts\\python.exe scripts\\jira_pull.py --project ABC      # задачи проекта ABC -> data\\jira\\ABC.json
  venv\\Scripts\\python.exe scripts\\jira_pull.py --all              # все проекты из JIRA_PROJECT_KEYS
  venv\\Scripts\\python.exe scripts\\jira_pull.py --project ABC --since 2026-10-01 --limit 200

Настройки — в .env: JIRA_BASE_URL, JIRA_API_TOKEN, для облачной Jira ещё JIRA_EMAIL (образец — .env.example).
В Jira ничего не пишет и базу портала не трогает; выгрузка лежит в data\\jira (в git не попадает).
План подключения — docs/plans/JIRA-INTEGRATION-PLAN.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def _save(out_dir: Path, key: str, base_url: str, issues: list[dict]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{key}.json"
    payload = {"project": key, "jira": base_url, "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "count": len(issues), "issues": issues}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _summary(issues: list[dict]) -> str:
    by_status = Counter(row["portal_status"] for row in issues)
    labels = {"open": "открыто", "in_progress": "в работе", "done": "сделано"}
    parts = [f"{labels.get(name, name)} {count}" for name, count in by_status.most_common()]
    no_assignee = sum(1 for row in issues if not row["assignee"])
    no_deadline = sum(1 for row in issues if not row["deadline_at"])
    return f"{', '.join(parts) or 'задач нет'}; без исполнителя {no_assignee}, без срока {no_deadline}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", action="append", default=[], help="ключ проекта Jira (можно несколько раз)")
    ap.add_argument("--all", action="store_true", help="все проекты из JIRA_PROJECT_KEYS")
    ap.add_argument("--since", default=None, help="только изменённые с этой даты, ГГГГ-ММ-ДД")
    ap.add_argument("--limit", type=int, default=None, help="не больше стольких задач на проект")
    ap.add_argument("--show", type=int, default=5, help="сколько задач показать на экране (по умолчанию 5)")
    args = ap.parse_args(argv)

    from app.app.config import get_settings
    from app.app.services.jira_client import JiraClient, JiraError, JiraNotConfigured, project_keys

    settings = get_settings()
    try:
        client = JiraClient.from_settings(settings)
    except JiraNotConfigured as exc:
        print(f"✗ {exc}")
        print("  Откройте .env и заполните (образец в .env.example, блок «Jira»):")
        print("    JIRA_BASE_URL=https://адрес-вашей-jira")
        print("    JIRA_API_TOKEN=ключ")
        print("    JIRA_EMAIL=почта          # только для облачной Jira (*.atlassian.net)")
        return 2

    keys = [k.strip().upper() for k in args.project if k.strip()]
    if args.all:
        keys += [k for k in project_keys(settings) if k not in keys]
    try:
        with client:
            me = client.myself()
            print(f"✓ Jira отвечает: {client.base_url} ({client.mode}); ключ принадлежит: {me['name'] or me['account'] or '—'}")
            if not keys:
                projects = client.projects()
                print(f"Доступно проектов: {len(projects)}")
                for row in projects:
                    print(f"  {row['key']:<12} {row['name']}")
                if args.all:
                    print("JIRA_PROJECT_KEYS в .env пуст: перечислите ключи через запятую или укажите --project КЛЮЧ")
                elif projects:
                    print(f"Дальше: scripts\\jira_pull.py --project {projects[0]['key']}")
                return 0
            out_dir = settings.data_dir / "jira"
            for key in keys:
                issues = client.issues(key, updated_since=args.since, limit=args.limit)
                path = _save(out_dir, key, client.base_url, issues)
                print(f"✓ {key}: задач {len(issues)} — {_summary(issues)}")
                for row in issues[: max(0, args.show)]:
                    print(f"    {row['key']:<12} [{row['status'] or '—'}] {row['summary'][:90]}"
                          f"{' — ' + row['assignee'] if row['assignee'] else ''}{' до ' + row['deadline_at'] if row['deadline_at'] else ''}")
                print(f"  сохранено: {path}")
    except JiraError as exc:
        print(f"✗ {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
