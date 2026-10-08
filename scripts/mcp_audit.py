"""Аудит проектного MCP на живых данных (docs/plans/MCP-KB-PLAN.md): что и сколько получает внешний агент.

Вызывает инструменты так же, как адрес `/mcp/projects/<id>` (только чтение), и печатает объёмы и признаки полноты — без
содержимого материалов: отчёт можно показывать. Проверки приёмки:
  - ни один ответ не больше порции (`MCP_PAGE_BYTES`, по умолчанию 60 КБ; с запасом 5 %);
  - текст каждого готового материала читается целиком: по курсору собранное сверяется с файлом на диске;
  - у готовых документов и переписок текст не пустой;
  - память решений отдаётся в режиме live и отклоняется в остальных;
  - инструкция при подключении называет права ключа.

  venv\\Scripts\\python.exe scripts\\mcp_audit.py 36            # отчёт в консоль
  venv\\Scripts\\python.exe scripts\\mcp_audit.py 18 36 --save  # и в data/eval/mcp_audit_<время>.md

Код выхода: 0 — все проверки пройдены, 1 — есть провалы.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEXT_FILES = {"call": ("_final.txt",), "document": ("_clean.txt",), "chat": ("_clean.txt",)}
MAX_PAGES = 200


def _setup():
    from app.app.config import get_settings
    from app.app.db import SessionLocal

    return get_settings(), SessionLocal()


def _token(db, project_id: int):
    from sqlalchemy import text

    row = db.execute(text("SELECT * FROM project_mcp_tokens WHERE project_id=:p AND is_active=1 ORDER BY last_used_at DESC"),
                     {"p": project_id}).mappings().first()
    return SimpleNamespace(**dict(row)) if row else SimpleNamespace(scope="read", files_access="read", rate_limit=None, token_prefix="audit",
                                                                    can_submit=False, id=0, agent_name="audit")


def _call(db, settings, project_id: int, token, name: str, args: dict) -> tuple[object, int, float, bool]:
    from app.app.mcp import project_protocol as pp

    started = time.monotonic()
    res = pp.call_tool(db, settings, project_id, name, args, token=token)
    body = res["content"][0]["text"]
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = body
    return data, len(body.encode("utf-8")), round(time.monotonic() - started, 1), bool(res.get("isError"))


def _material_kind(art) -> str:
    if art.file_type == "audio" or art.source_type == "call":
        return "call"
    return "chat" if art.file_type == "html" and art.source_type == "chat" else "document"


def _disk_text_len(settings, art) -> int:
    from app.app.services.storage import parsed_dir

    best = 0
    for suffix in ("_final.txt", "_clean.txt"):
        path = parsed_dir(settings, art.project_id, art.source_type) / f"{art.artifact_id}{suffix}"
        if path.is_file():
            best = max(best, len(path.read_text(encoding="utf-8", errors="replace")))
    return best


def _read_text_fully(db, settings, project_id, token, artifact_id: str, page_limit: int) -> tuple[int, int, bool, int, str]:
    """Собрать текст материала по курсору. Возвращает (символов, страниц, был_ли_признак_обрезки, наибольшая_порция, вид)."""
    got, pages, flagged, biggest, kind, cursor = 0, 0, False, 0, "", None
    while pages < MAX_PAGES:
        args = {"artifact_id": artifact_id, "kind": "text"}
        if cursor:
            args["cursor"] = cursor
        data, size, _, err = _call(db, settings, project_id, token, "get_artifact_content", args)
        pages += 1
        biggest = max(biggest, size)
        if err or not isinstance(data, dict):
            break
        kind = str(data.get("kind") or "")
        got += len(data.get("content") or "")
        flagged = flagged or bool(data.get("truncated"))
        cursor = data.get("cursor")
        if not cursor:
            break
    return got, pages, flagged, biggest, kind


def audit(project_id: int) -> tuple[list[str], list[str]]:
    from sqlalchemy import select

    from app.app.mcp import project_protocol as pp
    from app.app.models.artifact import Artifact
    from app.app.models.project import Project
    from app.app.services import memory_mode

    settings, db = _setup()
    page = int(getattr(settings, "mcp_page_bytes", 60_000) or 60_000)
    limit_bytes = int(page * 1.05)
    token = _token(db, project_id)
    project = db.get(Project, project_id)
    lines = [f"## Проект {project_id}", "", f"Ключ: права `{getattr(token, 'scope', '?')}`, файлы `{getattr(token, 'files_access', '?')}`; порция {page} байт.", ""]
    fails: list[str] = []

    init = pp.handle_jsonrpc(db, settings, project_id, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, token=token)
    instr = init["result"]["instructions"]
    lines.append(f"- instructions: {len(instr)} символов; инструментов {len(pp.MCP_TOOLS)}")
    if "твой ключ" not in instr.lower():
        fails.append(f"{project_id}: инструкция при подключении не называет права ключа")

    lines += ["", "| Инструмент | Байт | с | Ошибка | Признаки |", "|---|---|---|---|---|"]
    mode = memory_mode.mode_of(settings, project)
    calls = [("get_guide", {}), ("project_overview", {}), ("get_project_summary", {}), ("exchange_status", {}), ("list_artifacts", {"limit": 500}),
             ("list_entities", {"limit": 500}), ("list_conflicts", {"limit": 200}), ("list_notes", {"limit": 200}), ("get_knowledge_graph", {"limit": 500}),
             ("get_requirements", {"limit": 500}), ("get_tz", {}), ("get_changes", {"limit": 1000}), ("get_code_anchors", {}), ("get_conflicts", {}),
             ("files_catalog", {}), ("search_project", {"query": "сроки и решения по проекту", "limit": 30})]
    if any(t["name"] == "get_decisions" for t in pp.MCP_TOOLS):
        calls.append(("get_decisions", {}))
    for name, args in calls:
        data, size, sec, err = _call(db, settings, project_id, token, name, args)
        marks = []
        if isinstance(data, dict):
            for key in ("items", "results", "nodes"):
                if isinstance(data.get(key), list):
                    marks.append(f"{key}={len(data[key])}")
            if "total" in data:
                marks.append(f"total={data['total']}")
            if data.get("cursor"):
                marks.append("cursor")
            if data.get("error"):
                marks.append(f"error={data.get('detail') or data['error']}"[:60])
        elif isinstance(data, list):
            marks.append(f"элементов={len(data)}")
        lines.append(f"| `{name}` | {size} | {sec} | {'да' if err else ''} | {' '.join(marks)} |")
        if size > limit_bytes:
            fails.append(f"{project_id}: {name} — {size} байт больше порции {page}")
        if name == "get_decisions":
            if mode == "live" and err:
                fails.append(f"{project_id}: память решений в live, но get_decisions вернул ошибку")
            if mode != "live" and not err:
                fails.append(f"{project_id}: память в режиме {mode}, а get_decisions её отдал")

    lines += ["", "| Материал | Вид | Статус | На диске, симв. | Получено | Порций | Наиб. порция | Отдан вид |", "|---|---|---|---|---|---|---|---|"]
    for art in db.scalars(select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.id)).all():
        disk = _disk_text_len(settings, art)
        got, pages, flagged, biggest, kind = _read_text_fully(db, settings, project_id, token, art.artifact_id, page)
        lines.append(f"| `{art.artifact_id}` | {_material_kind(art)} {art.file_type} | {art.status} | {disk} | {got} | {pages} | {biggest} | {kind} |")
        if art.status != "done":
            continue
        if disk and got < disk * 0.99:
            fails.append(f"{project_id}: {art.artifact_id} — получено {got} из {disk} символов{' (с пометкой)' if flagged else ' молча'}")
        if not disk and not got:
            fails.append(f"{project_id}: {art.artifact_id} — у готового материала нет текста ни на диске, ни в MCP")
        if biggest > limit_bytes:
            fails.append(f"{project_id}: {art.artifact_id} — порция {biggest} байт больше {page}")
    lines += ["", f"Память решений: режим `{mode}`.", ""]
    db.close()
    return lines, fails


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("projects", nargs="+", type=int)
    ap.add_argument("--save", action="store_true", help="записать отчёт в data/eval/mcp_audit_<время>.md")
    args = ap.parse_args()
    report = [f"# Аудит MCP базы знаний — {datetime.now():%Y-%m-%d %H:%M}", ""]
    fails: list[str] = []
    for pid in args.projects:
        lines, bad = audit(pid)
        report += lines
        fails += bad
    report += ["## Итог", ""] + ([f"- ✗ {f}" for f in fails] or ["- ✓ все проверки пройдены"])
    text = "\n".join(report) + "\n"
    print(text)
    if args.save:
        out = ROOT / "data" / "eval" / f"mcp_audit_{datetime.now():%Y%m%d_%H%M}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"отчёт: {out.relative_to(ROOT)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
