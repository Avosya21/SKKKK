"""Живая сверка «Jira ↔ портал»: всё ли из Jira загружено в проекты портала полностью. Только чтение.

  venv\\Scripts\\python.exe scripts\\check_jira_sync.py                 # все привязанные проекты
  venv\\Scripts\\python.exe scripts\\check_jira_sync.py --project ABC   # одна привязка по ключу Jira
  venv\\Scripts\\python.exe scripts\\check_jira_sync.py --json data\\tmp\\jira_check_report.json

Для каждой привязки (``jira_project_links``): все ли задачи Jira есть в портале; совпадает ли документ каждой задачи
буква в букву с тем, что Jira отдаёт сейчас (все поля, комментарии, трудозатраты, история — ``jira_render``); все ли
вложения загружены и совпадают по размеру, что не загружено и почему; разобраны ли материалы конвейером и есть ли они в
поиске. Та же проверка — кнопка «Проверить полноту» на странице проекта (``GET /projects/{id}/jira/check``).
Ходит в Jira ключом из .env (JIRA_API_TOKEN); в Jira и в базу портала ничего не пишет. Код выхода 0 — всё сходится, 1 — есть расхождения.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", action="append", default=[], help="ключ проекта Jira (можно несколько раз); без него — все привязки")
    ap.add_argument("--json", default=str(ROOT / "data" / "tmp" / "jira_check_report.json"), help="куда записать отчёт")
    args = ap.parse_args(argv)

    from sqlalchemy import select

    import app.app.models  # noqa: F401
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.jira import JiraProjectLink
    from app.app.services import jira_import
    from app.app.services.jira_client import JiraClient, JiraError

    settings = get_settings()
    wanted = {k.strip().upper() for k in args.project if k.strip()}
    reports: list[dict] = []
    db = SessionLocal()
    try:
        links = [l for l in db.scalars(select(JiraProjectLink).order_by(JiraProjectLink.id)).all() if not wanted or l.jira_key in wanted]
        if not links:
            print("Привязок к Jira нет" + (f" для {', '.join(sorted(wanted))}" if wanted else "") + ".")
            return 0
        try:
            with JiraClient.from_settings(settings) as client:
                for link in links:
                    report = jira_import.check_link(db, settings, link, client)
                    reports.append(report)
                    mark = "✓" if report["ok"] else "✗"
                    print(f"{mark} {link.jira_key}: задач в Jira {report['jira_issues']}, в портале {report['portal_issues']}; "
                          f"комментариев {report['comments']}, трудозатрат {report['worklogs']}, записей истории {report['history']}, "
                          f"дополнительных полей {report['extra_fields']}; вложений в Jira {report['jira_attachments']}, "
                          f"файлов в портале {report['portal_files']}, не загружено {len(report['not_loaded'])}; разбор: {report['pipeline']}")
                    for item in report["not_loaded"]:
                        print(f"    не загружено ({item['issue']}): {item['reason']}")
                    for problem in report["problems"]:
                        print(f"    ✗ {problem}")
        except JiraError as exc:
            print(f"✗ {exc}")
            return 1
    finally:
        db.close()
    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"ok": all(r["ok"] for r in reports), "links": reports}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Отчёт: {out}")
    ok = all(r["ok"] for r in reports)
    print("Всё сходится." if ok else "Есть расхождения.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
