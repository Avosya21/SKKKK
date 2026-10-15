"""Подсказки реплик для пунктов созвонов без источника (2026-10-15, продолжение находки 91).

Модель при извлечении указывает номера реплик-источников (``turns``), но при сохранении пункта они выбрасывались: после
«Слить» номера перенумеровываются. Теперь пункт хранит моменты начала этих реплик (``turn_hint_sec``) — они не меняются.
Уже сохранённым пунктам моменты восстанавливаются из ответа модели ``structured/<artifact_id>_entities.json`` (сопоставление
по точному тексту пункта), затем пункты без источника перепривязываются с подсказкой (порог совпадения слов 0,25 вместо
0,4). Модель не вызывается. Пункт, получивший источник, снова проверяется автопроверкой (вердикт «нет источника»
перепроверяется сам).

    venv\\Scripts\\python scripts\\backfill_turn_hints.py                    # отчёт
    venv\\Scripts\\python scripts\\backfill_turn_hints.py --apply --queue    # копия БД, запись, повторная проверка
    ... --project 40
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TYPES = ("decision", "task", "open_question")
PLACE_KEYS = ("start_sec", "end_sec", "speaker", "speaker_source", "source_span_id", "text_hash", "needs_review", "source_status")


def norm(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def payload_hints(path: Path) -> dict[str, list[int]]:
    """Текст пункта из ответа модели → номера реплик."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, list[int]] = {}
    for key, field in (("decisions", "text"), ("tasks", "description"), ("open_questions", "text")):
        for item in data.get(key) or []:
            if isinstance(item, dict) and item.get("turns"):
                text = norm(item.get(field))
                if text:
                    out.setdefault(text, [int(t) for t in item["turns"] if str(t).lstrip("-").isdigit()])
    return out


def backup(db_path: Path) -> Path:
    target = db_path.parent / "backups" / f"app-before-turn-hints-{datetime.now():%Y-%m-%d_%H%M%S}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.execute(f"VACUUM INTO '{target.as_posix()}'")
    finally:
        con.close()
    return target


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--queue", action="store_true")
    ap.add_argument("--project", type=int, action="append", default=[])
    args = ap.parse_args()

    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.models.project import Project
    from app.app.services.auto_review import review_mode
    from app.app.services.entities import bind_call_source, load_meeting_proof, turn_hint_seconds
    from app.app.services.storage import structured_dir
    from app.app.services.transcript_turns import load_turns

    settings = get_settings()
    db_path = ROOT / "data" / "app.db"
    with SessionLocal() as db:
        q = select(Artifact).where(Artifact.status == "done", Artifact.source_type == "call", Artifact.file_type == "audio")
        if args.project:
            q = q.where(Artifact.project_id.in_(args.project))
        changes: list[tuple] = []
        report = []
        for art in db.scalars(q.order_by(Artifact.project_id, Artifact.id)).all():
            path = structured_dir(settings, art.project_id) / f"{art.artifact_id}_entities.json"
            if not path.is_file():
                continue
            hints = payload_hints(path)
            turns = load_turns(settings, art)
            replicas, _leaks = load_meeting_proof(settings, art)
            st = Counter()
            for ent in db.scalars(select(Entity).where(Entity.artifact_id == art.id, Entity.entity_type.in_(TYPES))).all():
                value = json.loads(ent.value_json or "{}")
                if not isinstance(value, dict) or value.get("message_id"):
                    continue
                text = norm(value.get("text") or value.get("description"))
                new_value = dict(value)
                if not value.get("turn_hint_sec") and text in hints:
                    sec = turn_hint_seconds(hints[text], turns)
                    if sec:
                        new_value["turn_hint_sec"] = sec
                        st["подсказка восстановлена"] += 1
                if ent.is_active and value.get("start_sec") is None:
                    st["без источника"] += 1
                    if new_value.get("turn_hint_sec"):
                        probe = {k: v for k, v in new_value.items() if k not in PLACE_KEYS}
                        bound = bind_call_source(probe, art, replicas, [], turns=turns, recorded_at=value.get("recorded_at"))
                        if bound is not None and bound.get("start_sec") is not None:
                            for key in PLACE_KEYS:
                                if key in bound:
                                    new_value[key] = bound[key]
                                else:
                                    new_value.pop(key, None)
                            st["получили источник"] += 1
                if new_value != value:
                    changes.append((ent, new_value, new_value.get("start_sec") is not None and value.get("start_sec") is None))
            if st:
                report.append({"project": art.project_id, "artifact": art.id, "file": art.original_filename, **st})
        for row in report:
            print(row)
        total = Counter()
        for row in report:
            total.update({k: v for k, v in row.items() if isinstance(v, int) and k not in ("project", "artifact")})
        print("ИТОГО:", dict(total), "созвонов:", len(report))
        if not args.apply:
            print("Только отчёт. Применить: --apply (копия БД делается сама).")
            return 0
        print("Копия БД:", backup(db_path))
        touched: set[int] = set()
        for ent, value, newly_bound in changes:
            value["turn_hints_backfill"] = datetime.now().isoformat(timespec="seconds")
            ent.value_json = json.dumps(value, ensure_ascii=False)
            if newly_bound:
                touched.add(ent.artifact_id)
        db.commit()
        from app.app.services import contract as ctr

        ctr._cache.clear()
        print(f"Записано пунктов: {len(changes)}; получили источник: {sum(1 for c in changes if c[2])}; материалов: {len(touched)}")
        if args.queue and touched:
            from app.app.services.queue import enqueue_auto_review

            queued = 0
            for aid in sorted(touched):
                art = db.get(Artifact, aid)
                mode = review_mode(settings, db.get(Project, art.project_id))
                if mode in ("shadow", "live") and enqueue_auto_review(db, aid, scope="artifact", mode=mode):
                    queued += 1
            db.commit()
            print(f"Повторная проверка в очереди: {queued} материалов")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
