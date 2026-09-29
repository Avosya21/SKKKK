"""Разобрать проект локальной моделью целиком: пусть система решит всё, что может, и оставит человеку только спорное.

    venv\\Scripts\\python scripts\\review_project.py --project 18                 # тень: решения записываются, не применяются
    venv\\Scripts\\python scripts\\review_project.py --project 18 --mode live     # применить (всё обратимо кнопкой «Вернуть»)
    venv\\Scripts\\python scripts\\review_project.py --all --reextract --mode live --skip 18 29   # привести в порядок все проекты

Шаги: даты материалов (как у новой загрузки; нет фактической — дата загрузки) → с ``--reextract`` переизвлечение
требований по новой схеме с контекстом проекта (ручные решения переносятся, темы собираются заново; как кнопка
«Переизвлечь требования», но без очереди) → привязка пунктов переписки к сообщениям (без модели) → проверка требований
по источнику (повторно — там, где источник раньше не находился) → раскладка по темам и склейка тем одной потребности →
дубли и устаревшее по темам → конфликты → сводка «что осталось человеку». В режиме live теневые решения применяются без
повторного вызова модели. Запускать, когда очередь воркера пуста (одна GPU): скрипт сам проверит и не начнёт, если идёт
расшифровка или разбор.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _live_projection(db, settings, project_id: int, pending_conflicts: list[dict]) -> dict:
    """Что останется человеку, если применить теневые решения: «не уверена», контрольная выборка, конфликты без решения."""
    from app.app.services import contract as ctr
    from app.app.services.requirement_review import hidden_state

    items = ctr.get_contract(db, settings, project_id).files["requirements"]["items"]
    out = {"requirements_total": len(items), "auto_confirmed": 0, "auto_rejected": 0, "hidden_superseded_or_duplicate": 0,
           "human_confirmed": 0, "wait_unsure": 0, "wait_audit": 0, "wait_no_verdict": 0}
    for r in items:
        review = r.get("model_review") or {}
        if r["verification"] == "human_confirmed":
            out["human_confirmed"] += 1
        elif hidden_state(r):
            out["hidden_superseded_or_duplicate"] += 1
        elif review.get("verdict") == "reject":
            out["auto_rejected"] += 1
        elif review.get("verdict") == "confirm":
            out["auto_confirmed"] += 1
            out["wait_audit"] += bool(review.get("audit"))
        elif review.get("verdict") == "unsure":
            out["wait_unsure"] += 1
        else:
            out["wait_no_verdict"] += 1
    decided = [c for c in pending_conflicts if (c.get("model_review") or {}).get("verdict") in ("newer_wins", "older_wins", "not_conflict")]
    out["conflicts_auto"] = len(decided)
    out["conflicts_wait"] = len(pending_conflicts) - len(decided)
    out["requirements_wait_total"] = out["wait_unsure"] + out["wait_audit"] + out["wait_no_verdict"]
    return out


def _sum(stats_list: list[dict]) -> dict:
    out: dict = {}
    for stats in stats_list:
        for key, value in stats.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = out.get(key, 0) + value
    return out


def _reextract(db, settings, arts) -> dict:
    """Извлечение v2 с контекстом проекта для каждого материала (как ``requirements_reextract``, но без очереди)."""
    from sqlalchemy import update

    from app.app.models.entity import Entity, Topic
    from app.app.services import contract as ctr
    from app.app.services.entities import reextract_artifact_entities
    from app.app.services.graph import seed_graph
    from app.app.services.storage import artifact_knowledge_text

    if not arts:
        return {"materials": 0}
    project_id = arts[0].project_id
    db.execute(update(Entity).where(Entity.artifact_id.in_([a.id for a in arts])).values(topic_id=None))
    db.query(Topic).filter(Topic.project_id == project_id).delete(synchronize_session=False)
    db.commit()
    stats = {"materials": len(arts), "ok": 0, "failed": 0, "tasks": 0}
    for art in arts:
        started = time.monotonic()
        try:
            result = reextract_artifact_entities(db, settings, art)
            if result.get("ok"):
                seed_graph(db, settings, art, artifact_knowledge_text(settings, art) or art.transcription_text or "")
                db.commit()
        except Exception as exc:  # noqa: BLE001 — материал останется со старыми пунктами, остальные разберутся
            db.rollback()
            result = {"ok": False, "error": str(exc)[:200]}
        stats["ok" if result.get("ok") else "failed"] += 1
        stats["tasks"] += int(result.get("tasks") or 0)
        print(f"   переизвлечено: {art.original_filename} — {'ок' if result.get('ok') else result.get('error')} "
              f"({time.monotonic() - started:.0f} с)", flush=True)
    ctr._cache.pop(project_id, None)
    return stats


def _summary(db, settings, project_id: int) -> dict:
    """Итоговая выжимка проекта по текущим пунктам (после переизвлечения в ней старые)."""
    from app.app.services.requirements_reextract import rebuild_project_summary

    started = time.monotonic()
    rebuild_project_summary(db, settings, project_id)
    return {"summary_seconds": round(time.monotonic() - started, 1)}


def review_one(db, settings, project_id: int, mode: str, *, reextract: bool) -> dict:
    from material_dates import fill_dates

    from app.app.services import contract as ctr
    from app.app.services.entities import rebind_chat_entities
    from app.app.services.requirement_review import review_list
    from app.app.services.requirements_reextract import project_materials
    from app.app.services.review_conflicts import conflict_rows, review_artifact_conflicts
    from app.app.services.review_relevance import review_topics
    from app.app.services.review_requirements import review_artifact_requirements
    from app.app.services.topics import assign_topics, merge_topics

    started = time.monotonic()
    report: dict = {"project": project_id, "mode": mode}
    report["dates"] = fill_dates(db, settings, project_id, apply=True, out=lambda *_a: None)
    arts = project_materials(db, project_id)
    report["materials"] = len(arts)
    if reextract:
        report["reextract"] = _reextract(db, settings, arts)
    report["chat_rebound"] = {a.artifact_id: rebind_chat_entities(db, settings, a) for a in arts if a.file_type != "audio"}
    report["chat_rebound"] = {k: v for k, v in report["chat_rebound"].items() if any(v.values())}
    req = []
    for art in arts:
        req.append(review_artifact_requirements(db, settings, art, mode))
        db.commit()
    report["requirements"] = _sum(req)
    report["topics"] = assign_topics(db, settings, project_id)
    report["topics_merge"] = merge_topics(db, settings, project_id)
    report["relevance"] = review_topics(db, settings, project_id, mode)
    cft = []
    for art in arts:
        cft.append(review_artifact_conflicts(db, settings, art, mode))
        db.commit()
    report["conflicts"] = _sum(cft)
    ctr._cache.pop(project_id, None)
    if reextract:
        report["summary_rebuilt"] = _summary(db, settings, project_id)
    queue = review_list(db, settings, project_id, verification="needs_human", limit=200)
    pending_conflicts = conflict_rows(db, project_id, view="pending")
    report["left_for_human"] = {"requirements_waiting": queue["counts"]["needs_human"], "superseded": queue["counts"]["superseded"],
                                "merged": queue["counts"]["merged"], "conflicts_waiting": len(pending_conflicts)}
    if mode == "shadow":
        report["if_live"] = _live_projection(db, settings, project_id, pending_conflicts)
    report["seconds"] = round(time.monotonic() - started, 1)
    report["_waiting"] = queue["items"][:15]
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--project", type=int)
    group.add_argument("--all", action="store_true", help="все активные проекты")
    ap.add_argument("--mode", choices=("shadow", "live"), default="shadow")
    ap.add_argument("--reextract", action="store_true", help="переизвлечь требования по новой схеме (долго: модель на каждый материал)")
    ap.add_argument("--skip", type=int, nargs="*", default=[], help="номера проектов, которые не трогать (с --all)")
    ap.add_argument("--summary-only", action="store_true", help="только пересобрать итоговую выжимку по текущим пунктам (без проверок)")
    args = ap.parse_args()

    from sqlalchemy import func, select

    from app.app.config import get_settings
    from app.app.db import SessionLocal
    from app.app.models.job import Job
    from app.app.models.project import Project

    settings = get_settings()
    db = SessionLocal()
    try:
        busy = db.scalar(select(func.count()).select_from(Job).where(Job.status.in_(("queued", "running")))) or 0
        if busy:
            print(f"в очереди воркера {busy} задач — не начинаю: одна GPU, расшифровке и разбору нельзя мешать", file=sys.stderr)
            return 2
        if args.project:
            ids = [args.project]
        else:
            ids = [p for p in db.scalars(select(Project.id).where(Project.is_active.is_(True)).order_by(Project.id)).all()
                   if p not in set(args.skip)]
        for project_id in ids:
            print(f"\n=== #{project_id} {db.get(Project, project_id).name}", flush=True)
            if args.summary_only:
                print(json.dumps(_summary(db, settings, project_id), ensure_ascii=False), flush=True)
                continue
            report = review_one(db, settings, project_id, args.mode, reextract=args.reextract)
            waiting = report.pop("_waiting")
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str), flush=True)
            print("\nЖдут человека (первые 15):")
            for row in waiting:
                reason = ((row.get("model_review") or {}).get("reason") or "")[:110]
                print(f"  - {row['statement'][:110]}\n      модель: {reason}", flush=True)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
