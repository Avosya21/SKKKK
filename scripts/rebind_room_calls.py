"""Перепривязка пунктов созвонов портала к репликам после исправления сопоставления (2026-10-15).

До исправления ``entities._match_replica`` брала первую реплику-доказательство, текст которой «содержится» в пункте:
короткие «По», «Ну», «Да» содержатся почти в любой фразе, и пункты созвона привязывались к ним — с чужим говорящим и
моментом. Проверка модели (auto_review, live) видела не ту реплику и выключала пункты как «не сказано» (``not_said``).

Скрипт без модели перепривязывает пункты (decision / task / fact / open_question) созвонов с репликами-доказательствами
дорожек: меняются только поля места (момент, говорящий, дорожка). Если привязка изменилась и пункт был отклонён моделью
как «не сказано», вердикт модели снимается (копия — ``value_json.model_review_before_rebind``), пункт снова активен и ждёт
проверки. Решения человека и отклонения как дубль не трогаются.

    venv\\Scripts\\python scripts\\rebind_room_calls.py                    # отчёт, ничего не пишет
    venv\\Scripts\\python scripts\\rebind_room_calls.py --apply            # копия БД, перепривязка, снятие «не сказано»
    venv\\Scripts\\python scripts\\rebind_room_calls.py --apply --queue    # + повторная проверка в очередь воркера
    ... --project 40                                                        # только проект
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

TYPES = ("decision", "task", "fact", "open_question")
PLACE_KEYS = ("start_sec", "end_sec", "speaker", "speaker_source", "source_span_id", "text_hash", "needs_review", "source_status")


def backup(db_path: Path) -> Path:
    target = db_path.parent / "backups" / f"app-before-rebind-room-calls-{datetime.now():%Y-%m-%d_%H%M%S}.db"
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
    ap.add_argument("--queue", action="store_true", help="поставить повторную проверку материалов в очередь воркера")
    ap.add_argument("--project", type=int, action="append", default=[])
    args = ap.parse_args()

    from sqlalchemy import select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.artifact import Artifact
    from app.app.models.entity import Entity
    from app.app.models.project import Project
    from app.app.services.auto_review import review_mode
    from app.app.services.entities import _has_span_replicas, bind_call_source, load_meeting_proof
    from app.app.services.transcript_turns import load_turns

    settings = get_settings()
    db_path = ROOT / "data" / "app.db"
    with SessionLocal() as db:
        q = select(Artifact).where(Artifact.status == "done", Artifact.source_type == "call", Artifact.file_type == "audio")
        if args.project:
            q = q.where(Artifact.project_id.in_(args.project))
        plan: list[tuple] = []
        report: list[dict] = []
        for art in db.scalars(q.order_by(Artifact.project_id, Artifact.id)).all():
            replicas, _leaks = load_meeting_proof(settings, art)
            if not _has_span_replicas(replicas):
                continue
            turns = load_turns(settings, art)
            stats = Counter()
            for ent in db.scalars(select(Entity).where(Entity.artifact_id == art.id, Entity.entity_type.in_(TYPES))).all():
                value = json.loads(ent.value_json or "{}")
                if not isinstance(value, dict) or value.get("speaker_source") not in ("track", None):
                    continue
                probe = {k: v for k, v in value.items() if k not in PLACE_KEYS}
                new = bind_call_source(probe, art, replicas, [], turns=turns, recorded_at=value.get("recorded_at"))
                if new is None:
                    continue
                stats["пунктов"] += 1
                if new.get("start_sec") == value.get("start_sec") and new.get("speaker") == value.get("speaker"):
                    continue
                stats["привязка изменилась"] += 1
                review = value.get("model_review") if isinstance(value.get("model_review"), dict) else {}
                reset = (review.get("verdict") == "reject" and review.get("reject_kind") == "not_said" and not review.get("undone")
                         and not value.get("human_review"))
                stats["вернуть из «не сказано»"] += int(reset)
                plan.append((ent, value, new, reset))
            if stats:
                report.append({"project": art.project_id, "artifact": art.id, "file": art.original_filename, **stats})
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
        for ent, value, new, reset in plan:
            for key in PLACE_KEYS:
                if key in new:
                    value[key] = new[key]
                else:
                    value.pop(key, None)
            value["rebind_room_call"] = datetime.now().isoformat(timespec="seconds")
            if reset:
                value["model_review_before_rebind"] = value.pop("model_review")
                ent.is_active = True
                ent.needs_review = True
                touched.add(ent.artifact_id)
            ent.value_json = json.dumps(value, ensure_ascii=False)
        db.commit()
        from app.app.services import contract as ctr

        ctr._cache.clear()
        print(f"Перепривязано пунктов: {len(plan)}; возвращено на проверку: {sum(1 for p in plan if p[3])}; материалов: {len(touched)}")
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
