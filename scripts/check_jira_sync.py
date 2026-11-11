# -*- coding: utf-8 -*-
"""Сверка «живая Jira <-> локальный портал»: что подтянулось, полностью ли и актуально ли.

Запуск из корня репозитория:  venv\\Scripts\\python.exe scripts\\check_jira_sync.py [out.json]
По умолчанию отчёт пишет в data/tmp/jira_check_report.json.
Выходной код 0 — всё сходится; 1 — найдены расхождения (в отчёте).

Что сверяется по каждой привязке проекта:
- список задач Jira против jira_items(kind='issue') (топ-N свежих — то, что портал обязан иметь);
- отметки updated (стали ли задачи в Jira «новее» — нужна новая синхронизация);
- вложения живых задач против jira_items(kind='attachment'), файлы на диске и их размеры;
- содержимое md-документов задач: описание, комментарии, список вложений;
- состояние конвейера: статусы артефактов и незавершённые process_artifact-заказы.
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


def norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def main():
    from app.app.config import get_settings
    from app.app.services.jira_client import JiraClient

    settings = get_settings()
    conn = sqlite3.connect("file:data/app.db?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row

    links = [dict(r) for r in conn.execute("SELECT id, jira_key, project_id FROM jira_project_links")]
    report = {"max_issues": settings.jira_import_max_issues, "media": settings.jira_import_media, "projects": {}}
    problems: list[str] = []

    client = JiraClient.from_settings(settings)
    with client:
        for link in links:
            key = link["jira_key"]
            pr: dict = {"live_issues": 0}
            report["projects"][key] = pr

            listing = list(client.iter_issues(key))
            live = {r["key"]: r for r in listing}
            pr["live_issues"] = len(live)
            maxn = int(settings.jira_import_max_issues)
            expected = {r["key"] for r in listing[:maxn]}  # свежие сверху — то, что портал обязан иметь

            db_rows = [dict(r) for r in conn.execute(
                "SELECT ref, updated, artifact_id FROM jira_items WHERE link_id=? AND kind='issue'", (link["id"],))]
            db_issues = {r["ref"]: r for r in db_rows}

            pr["db_issues"] = len(db_issues)
            missing = sorted(expected - set(db_issues))
            extra = sorted(set(db_issues) - set(live))
            pr["missing_in_portal"] = missing
            pr["gone_from_jira"] = extra  # ожидаемо: Jira их не удаляет молча; фиксируем
            if missing:
                problems.append(f"{key}: в портале нет задач из топ-{maxn} Jira: {', '.join(missing)}")

            stale = [k for k, r in db_issues.items() if live.get(k) and str(live[k]["updated"] or "") != str(r["updated"] or "")]
            pr["stale_updated"] = sorted(stale)
            if stale:
                problems.append(f"{key}: в Jira изменились задачи (нужна новая синхронизация): {', '.join(sorted(stale))}")

            # вложения: живые по всем подтянутым задачам против jira_items(attachment)
            live_att = {}
            raws = {}
            for k in sorted(set(expected) & set(db_issues)):
                if db_issues[k]["updated"] == str(live[k]["updated"] or ""):
                    raw = client.issue_raw(k)
                    raws[k] = raw
                    fields = raw.get("fields") or {}
                    for a in (fields.get("attachment") or []):
                        if isinstance(a, dict) and a.get("id"):
                            live_att[str(a["id"])] = {"issue": k, "filename": str(a.get("filename")), "size": int(a.get("size") or 0),
                                                      "created": str(a.get("created") or "")}
            db_atts = [dict(r) for r in conn.execute(
                """SELECT i.ref, i.issue_key, i.updated AS att_updated, i.artifact_id, i.note, a.file_path, a.original_filename, a.status AS art_status
                   FROM jira_items i LEFT JOIN artifacts a ON a.id=i.artifact_id WHERE i.link_id=? AND i.kind='attachment'""", (link["id"],))]
            db_att_ids = {r["ref"] for r in db_atts}
            pr["live_attachments"] = len(live_att)
            pr["db_attachments"] = len(db_atts)
            att_missing = sorted(set(live_att) - db_att_ids)
            att_extra = sorted(db_att_ids - set(live_att))
            pr["attachments_missing_in_portal"] = [f"{live_att[i]['issue']}:{live_att[i]['filename']}" for i in att_missing]
            pr["attachments_not_in_jira"] = att_extra
            if att_missing:
                problems.append(f"{key}: вложения не зарегистрированы: {pr['attachments_missing_in_portal']}")

            # файлы на диске и размеры
            file_problems, files_ok, skipped = [], 0, []
            for r in db_atts:
                live_a = live_att.get(r["ref"])
                if r["artifact_id"] is None:
                    skipped.append(f"{r['issue_key']}:{(live_a or {}).get('filename', '?')} -> {r['note'] or 'без причины'}")
                    continue
                path = r["file_path"]
                p = path if os.path.isabs(path) else os.path.join(ROOT, path.replace("/", os.sep))
                if not (os.path.isfile(p) and os.path.getsize(p) > 0):
                    file_problems.append(f"{r['issue_key']}: файл отсутствует: {path}")
                    continue
                size = os.path.getsize(p)
                if live_a and size != live_a["size"]:
                    file_problems.append(f"{r['issue_key']}:{live_a['filename']}: размер на диске {size} != в Jira {live_a['size']}")
                files_ok += 1
            pr["attachment_files_on_disk"] = files_ok
            pr["attachments_skipped"] = skipped
            if file_problems:
                problems.extend(f"{key}: " + p for p in file_problems)

            # содержимое документов задач: описание, комментарии, вложения, связи
            doc_problems = []
            checked = 0
            for k, raw in raws.items():
                art_id = db_issues[k]["artifact_id"]
                row = conn.execute("SELECT file_path FROM artifacts WHERE id=?", (art_id,)).fetchone()
                if not row:
                    doc_problems.append(f"{k}: артефакт {art_id} не найден")
                    continue
                path = row["file_path"]
                p = path if os.path.isabs(path) else os.path.join(ROOT, path.replace("/", os.sep))
                if not os.path.isfile(p):
                    doc_problems.append(f"{k}: документ отсутствует на диске: {path}")
                    continue
                text = open(p, encoding="utf-8").read()
                ntext = norm_ws(text)
                fields = raw.get("fields") or {}
                desc = fields.get("description")
                desc_txt = (desc if isinstance(desc, str) else str(desc or "")).strip()
                m = re.search(r"## Описание\n(.*?)(\n## |\Z)", text, flags=re.S)
                doc_desc = norm_ws(m.group(1)) if m else ""
                if desc_txt and norm_ws(desc_txt) != doc_desc:
                    # допускаем только различия в пробелах; иначе — расхождение по смыслу
                    doc_problems.append(f"{k}: описание в документе расходится с Jira")
                block = fields.get("comment") or {}
                comments = [c for c in (block.get("comments") or []) if isinstance(c, dict)]
                mc = re.search(r"## Комментарии \((\d+)\)", text)
                if comments and not mc:
                    doc_problems.append(f"{k}: раздел комментариев отсутствует, в Jira их {len(comments)}")
                elif comments and int(mc.group(1)) != len(comments):
                    doc_problems.append(f"{k}: комментариев в документе {mc.group(1)}, в Jira {len(comments)}")
                for c in comments:
                    body = norm_ws(c.get("body"))
                    if body and body[:60] not in ntext:
                        doc_problems.append(f"{k}: комментарий от {c.get('created','?')[:10]} не найден в документе (начало: {body[:40]!r})")
                atts = [a for a in (fields.get("attachment") or []) if isinstance(a, dict)]
                ma = re.search(r"## Вложения\n(.*?)(\n## |\Z)", text, flags=re.S)
                doc_atts = re.findall(r"- (.+?) \(\d+ КБ", ma.group(1)) if ma else []
                for a in atts:
                    fn = str(a.get("filename") or "")
                    if fn and not any(norm_ws(fn) == norm_ws(d) for d in doc_atts):
                        doc_problems.append(f"{k}: вложение {fn} не указано в документе задачи")
                checked += 1
            pr["documents_checked"] = checked
            pr["document_problems"] = doc_problems[:40]
            if doc_problems:
                problems.extend(f"{key}: " + p for p in doc_problems[:40])

            # состояние обработки конвейером
            art_status = [dict(r) for r in conn.execute(
                """SELECT a.status, count(*) n FROM jira_items i JOIN artifacts a ON a.id=i.artifact_id WHERE i.link_id=? GROUP BY a.status""", (link["id"],))]
            pr["artifacts_by_status"] = art_status
            pend_jobs = [dict(r) for r in conn.execute(
                """SELECT j.id, j.status, j.kind, j.phase, j.attempts, a.original_filename FROM jobs j
                   JOIN artifacts a ON a.id=j.artifact_id
                   WHERE j.status NOT IN ('done','skipped') AND EXISTS (SELECT 1 FROM jira_items i WHERE i.link_id=? AND i.artifact_id=a.id)""", (link["id"],))]
            pr["jira_jobs_not_finished"] = pend_jobs[:20]

    # сущности/задачи, извлечённые конвейером из Jira-документов
    cols = [r[1] for r in conn.execute("PRAGMA table_info(entities)")]
    ent_rows = []
    if "entities" in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
        sel_extra = ""
        where = """WHERE e.artifact_id IN (SELECT artifact_id FROM jira_items WHERE kind='issue')"""
        cols_sel = ", ".join(c for c in ("id", "entity_type", "artifact_id", "project_id") if c in cols)
        ent_rows = [dict(r) for r in conn.execute(f"SELECT {cols_sel} FROM entities e {where} LIMIT 50")]
    report["entities_from_jira_docs"] = ent_rows[:30]

    report["problems"] = problems
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "tmp", "jira_check_report.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"report -> {out_path}")
    if problems:
        print(f"РАСХОЖДЕНИЙ: {len(problems)}")
        for p in problems[:60]:
            print(" -", p)
        return 1
    print("Всё сходится: задачи, вложения и документы совпадают с Jira.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
