"""Аудит целостности данных (пункт плана 4.9): осиротевшие строки и файлы, устаревший индекс поиска. Только чтение.

    venv\\Scripts\\python scripts\\audit_integrity.py [--db data\\app.db] [--data data] [--json]
    venv\\Scripts\\python scripts\\audit_integrity.py --requeue-stale    # устаревший индекс — в очередь воркера

Код выхода 1, если найдены «сироты» (строки без родителя, векторы без чанка/сущности, папки проектов без проекта)
или индекс материала не совпадает с нарезкой его текущего текста (стенограмму переписали после индексации — перепрогон,
имена спикеров, — или изменилась нарезка). С ``--requeue-stale`` такие материалы ставятся задачей ``refresh_knowledge``
(индекс, у созвона ещё и выжимка) — единственная запись в базу.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# (название, SQL, что означает найденная строка)
CHECKS = [
    ("chunks без артефакта", "SELECT c.id FROM chunks c LEFT JOIN artifacts a ON a.id=c.artifact_id WHERE a.id IS NULL", "чанк удалённого артефакта"),
    ("chunks без проекта", "SELECT c.id FROM chunks c LEFT JOIN projects p ON p.id=c.project_id WHERE p.id IS NULL", "чанк удалённого проекта"),
    ("векторы чанков без чанка", "SELECT r.rowid FROM chunk_vectors_rowids r LEFT JOIN chunks c ON c.id=r.rowid WHERE c.id IS NULL", "вектор поиска без текста"),
    ("чанки без вектора", "SELECT c.id FROM chunks c LEFT JOIN chunk_vectors_rowids r ON r.rowid=c.id WHERE r.rowid IS NULL", "чанк недоступен векторному поиску"),
    ("entities без артефакта", "SELECT e.id FROM entities e LEFT JOIN artifacts a ON a.id=e.artifact_id WHERE a.id IS NULL", "сущность удалённого артефакта"),
    ("векторы сущностей без сущности", "SELECT v.entity_id FROM entity_vectors v LEFT JOIN entities e ON e.id=v.entity_id WHERE e.id IS NULL", "вектор без сущности"),
    ("links без сущности-источника", "SELECT l.id FROM links l LEFT JOIN entities e ON e.id=l.source_entity_id WHERE e.id IS NULL", "связь в никуда"),
    ("links без сущности-цели", "SELECT l.id FROM links l LEFT JOIN entities e ON e.id=l.target_entity_id WHERE e.id IS NULL", "связь в никуда"),
    ("resolutions без связи", "SELECT r.id FROM resolutions r LEFT JOIN links l ON l.id=r.link_id WHERE l.id IS NULL", "решение по удалённой связи"),
    ("jobs без артефакта", "SELECT j.id FROM jobs j LEFT JOIN artifacts a ON a.id=j.artifact_id WHERE a.id IS NULL", "задание удалённого артефакта"),
    ("asr_runs без артефакта", "SELECT r.id FROM asr_runs r LEFT JOIN artifacts a ON a.id=r.artifact_id WHERE a.id IS NULL", "запуск ASR удалённого артефакта"),
    ("artifact_speaker_links без артефакта", "SELECT s.id FROM artifact_speaker_links s LEFT JOIN artifacts a ON a.artifact_id=s.artifact_id WHERE a.id IS NULL", "спикер удалённого артефакта"),
    ("артефакты без проекта", "SELECT a.id FROM artifacts a LEFT JOIN projects p ON p.id=a.project_id WHERE p.id IS NULL", "артефакт удалённого проекта"),
    ("встречи без проекта", "SELECT m.id FROM meetings m LEFT JOIN projects p ON p.id=m.project_id WHERE p.id IS NULL", "встреча удалённого проекта"),
    ("участники без встречи", "SELECT x.id FROM meeting_participants x LEFT JOIN meetings m ON m.id=x.meeting_id WHERE m.id IS NULL", "участник удалённой встречи"),
    ("chat_messages без проекта", "SELECT c.id FROM chat_messages c LEFT JOIN projects p ON p.id=c.project_id WHERE p.id IS NULL", "чат удалённого проекта"),
    ("members без проекта", "SELECT m.id FROM project_members m LEFT JOIN projects p ON p.id=m.project_id WHERE p.id IS NULL", "доступ к удалённому проекту"),
    ("mcp-токены без проекта", "SELECT t.id FROM project_mcp_tokens t LEFT JOIN projects p ON p.id=t.project_id WHERE p.id IS NULL", "MCP-ключ удалённого проекта"),
    ("project_tasks без проекта", "SELECT t.id FROM project_tasks t LEFT JOIN projects p ON p.id=t.project_id WHERE p.id IS NULL", "фоновая задача удалённого проекта"),
    ("user_notes без проекта", "SELECT n.id FROM user_notes n LEFT JOIN projects p ON p.id=n.project_id WHERE p.id IS NULL", "заметка удалённого проекта"),
    ("changelog без проекта", "SELECT c.id FROM changelog c LEFT JOIN projects p ON p.id=c.project_id WHERE p.id IS NULL", "версия ТЗ удалённого проекта"),
]
STALE_CHECK = "индекс не совпадает с текущим текстом"


def stale_index(con: sqlite3.Connection, data_dir: Path) -> list[tuple[int, str]]:
    """Готовые материалы, чей индекс (``chunks``) не совпадает с нарезкой текущего текста той же функцией, что
    в ``indexing.index_artifact``: созвон — канон ``_final.txt``, документ — ``transcription_text``. (pk, artifact_id)."""
    from app.app.services.chunking import chunk_document, chunk_transcript
    from app.app.services.storage import artifact_knowledge_text

    settings = SimpleNamespace(data_dir=data_dir)
    stale: list[tuple[int, str]] = []
    rows = con.execute(
        "SELECT id, artifact_id, project_id, file_type, source_type, transcription_text, transcription_file_path "
        "FROM artifacts WHERE status = 'done' ORDER BY id"
    ).fetchall()
    for pk, aid, pid, file_type, source_type, text, text_path in rows:
        stored = [r[0] for r in con.execute("SELECT text FROM chunks WHERE artifact_id = ? ORDER BY chunk_index", (pk,))]
        if file_type == "audio":
            art = SimpleNamespace(artifact_id=aid, project_id=pid, source_type=source_type, transcription_text=text,
                                  transcription_file_path=text_path)
            canon = artifact_knowledge_text(settings, art)
            fresh = [c.text for c in chunk_transcript(canon)] if canon else []
        elif stored:
            fresh = [c.text for c in chunk_document((text or "").strip())]
        else:
            continue  # документ без индекса — файл обмена с агентом, в конвейер знаний не идёт
        if stored != fresh:
            stale.append((pk, aid))
    return stale


def requeue_stale(artifact_pks: list[int]) -> int:
    """Поставить материалы задачей ``refresh_knowledge`` (индекс, у созвона — и выжимка); ждущая задача не дублируется."""
    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.services.queue import KIND_REFRESH, enqueue_knowledge_refresh, running_worker_kinds

    if KIND_REFRESH not in running_worker_kinds(get_settings().data_dir):
        raise SystemExit("запущенный воркер не понимает refresh_knowledge — перезапустите сервер")
    db = SessionLocal()
    try:
        return sum(1 for pk in artifact_pks if enqueue_knowledge_refresh(db, pk) is not None)
    finally:
        db.close()


def run(db_path: Path, data_dir: Path) -> dict:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    findings: list[dict] = []
    for title, sql, meaning in CHECKS:
        try:
            ids = [r[0] for r in con.execute(sql)]
        except sqlite3.Error as exc:  # таблицы может не быть в старой схеме
            findings.append({"check": title, "error": str(exc)})
            continue
        if ids:
            findings.append({"check": title, "count": len(ids), "sample": ids[:5], "meaning": meaning})
    project_ids = {r[0] for r in con.execute("SELECT id FROM projects")}
    disk_orphans = []
    projects_dir = data_dir / "projects"
    if projects_dir.is_dir():
        for path in projects_dir.iterdir():
            if path.is_dir() and path.name.isdigit() and int(path.name) not in project_ids:
                disk_orphans.append(path.name)
    if disk_orphans:
        findings.append({"check": "папки проектов без проекта в БД", "count": len(disk_orphans), "sample": disk_orphans[:5],
                         "meaning": "файлы удалённого проекта остались на диске"})
    rec_root = data_dir / "meetings"
    room_names = {r[0] for r in con.execute("SELECT room_name FROM meetings")}
    rec_orphans = []
    if rec_root.is_dir():
        rec_orphans = [p.name for p in rec_root.iterdir() if p.is_dir() and p.name not in room_names]
    if rec_orphans:
        findings.append({"check": "записи комнат без встречи в БД", "count": len(rec_orphans), "sample": rec_orphans[:5],
                         "meaning": "сырые дорожки удалённой встречи остались на диске"})
    stale = stale_index(con, data_dir)
    if stale:
        findings.append({"check": STALE_CHECK, "count": len(stale), "sample": [aid for _pk, aid in stale[:5]],
                         "meaning": "поиск и чат видят старую версию материала; лечится --requeue-stale",
                         "artifact_pks": [pk for pk, _aid in stale]})
    con.close()
    return {"db": str(db_path), "problems": [f for f in findings if "count" in f or "error" in f], "checks": len(CHECKS) + 3}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "app.db")
    ap.add_argument("--data", type=Path, default=ROOT / "data")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--requeue-stale", action="store_true", help="устаревший индекс — задачей refresh_knowledge в очередь воркера")
    args = ap.parse_args()
    if args.requeue_stale and args.db.resolve() != (ROOT / "data" / "app.db").resolve():
        ap.error("--requeue-stale ставит задачи в рабочую базу сервера, --db другой базы с ним не сочетается")
    result = run(args.db, args.data)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Проверок: {result['checks']}, проблем: {len(result['problems'])}")
        for f in result["problems"]:
            print(f"- {f['check']}: {f.get('count', f.get('error'))} {f.get('sample', '')} — {f.get('meaning', '')}")
    stale = next((f for f in result["problems"] if f["check"] == STALE_CHECK), None)
    if args.requeue_stale and stale:
        print(f"Поставлено в очередь на переиндексацию: {requeue_stale(stale['artifact_pks'])} из {stale['count']}")
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
