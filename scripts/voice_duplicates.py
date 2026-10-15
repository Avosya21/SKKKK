"""Возможные дубли в общей базе голосов — список на проверку человеком; слияние только выбранных пар.

  venv\\Scripts\\python.exe scripts\\voice_duplicates.py                  # отчёт data/people/voice_duplicates.md
  venv\\Scripts\\python.exe scripts\\voice_duplicates.py --merge 12:57 3:40   # оставить 12, влить в него 57; копия БД

Пара попадает в список, если имена похожи (одно имя, «Игорь (Пилот)» = «Игорь Пилот», одно — начало другого) или голоса
близки (cosine ≥ ``VOICE_MIN``). Голоса двух разных учёток не предлагаются: это точно разные люди. Тёзки остаются в списке
с низким сходством голоса — решает человек. Отчёт содержит имена, поэтому лежит в ``data/`` (вне git).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.app.db import SessionLocal  # noqa: E402
from app.app.models.speaker_profile import ArtifactSpeakerLink, SpeakerProfile  # noqa: E402
from app.app.services.speaker_profiles import fold_speaker_display_name  # noqa: E402
from asr.speaker_registry import cosine, unpack_embedding  # noqa: E402

VOICE_MIN = 0.80
REPORT = ROOT / "data" / "people" / "voice_duplicates.md"  # data/people/ — вне git


def _name_relation(a: str, b: str) -> str | None:
    fa, fb = fold_speaker_display_name(a), fold_speaker_display_name(b)
    if not fa or not fb:
        return None
    if fa == fb:
        return "одно имя"
    wa, wb = fa.split(), fb.split()
    if wa == wb[: len(wa)] or wb == wa[: len(wb)]:
        return "одно — начало другого"
    if set(wa) == set(wb):
        return "те же слова"
    if wa[0] == wb[0]:
        return "одно имя (первое слово)"
    return None


def candidate_pairs(db) -> list[dict]:
    rows = db.query(SpeakerProfile).filter(SpeakerProfile.scope == "global").order_by(SpeakerProfile.id).all()
    vecs = {r.id: unpack_embedding(r.embedding) for r in rows}
    links: Counter = Counter()
    projects: dict[int, set] = defaultdict(set)
    for pid, project_id in db.query(ArtifactSpeakerLink.profile_id, ArtifactSpeakerLink.project_id).filter(
            ArtifactSpeakerLink.status == "confirmed", ArtifactSpeakerLink.profile_id.isnot(None)):
        links[pid] += 1
        projects[pid].add(project_id)
    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if a.user_id and b.user_id and a.user_id != b.user_id:
                continue
            score = cosine(vecs[a.id], vecs[b.id]) if len(vecs[a.id]) == len(vecs[b.id]) else 0.0
            rel = _name_relation(a.display_name, b.display_name)
            if rel is None and score < VOICE_MIN:
                continue
            out.append({"a": a, "b": b, "cos": score, "rel": rel or "—", "links": (links[a.id], links[b.id]),
                        "projects": (sorted(projects[a.id]), sorted(projects[b.id]))})
    # сначала самые вероятные дубли: похожие голоса, затем похожие имена
    return sorted(out, key=lambda p: (-p["cos"], p["a"].display_name.casefold()))


def write_report(pairs: list[dict]) -> Path:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Возможные дубли голосов — {datetime.now():%d.%m.%Y %H:%M}", "",
             "Слить: `scripts/voice_duplicates.py --merge ОСТАВИТЬ:ВЛИТЬ` (id). Тёзок с низким сходством голоса не сливать.", "",
             "| Оставить? | id | Голос A | созв. | id | Голос B | созв. | Сходство голоса | Имена |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for p in pairs:
        a, b = p["a"], p["b"]
        keep = a if (p["links"][0], a.user_id is not None) >= (p["links"][1], b.user_id is not None) else b
        lines.append(f"| {keep.id} | {a.id} | {a.display_name}{' (учётка)' if a.user_id else ''} | {p['links'][0]} "
                     f"| {b.id} | {b.display_name}{' (учётка)' if b.user_id else ''} | {p['links'][1]} | {int(round(p['cos'] * 100))} % | {p['rel']} |")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return REPORT


def merge(db, pairs: list[tuple[int, int]]) -> None:
    from app.app.config import get_settings
    from app.app.services.speaker_profiles import _absorb_profile, _refresh_match_views_after_cleanup

    db_path = get_settings().data_dir / "app.db"
    backup = db_path.parent / "backups" / f"app.db.bak-voices-{datetime.now():%Y%m%d-%H%M%S}"  # data/backups/ — вне git
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, backup)
    print("копия БД:", backup)
    for keep_id, dup_id in pairs:
        keep, dup = db.get(SpeakerProfile, keep_id), db.get(SpeakerProfile, dup_id)
        if keep is None or dup is None or keep.id == dup.id:
            print(f"пропуск {keep_id}:{dup_id} — нет такого голоса")
            continue
        if keep.user_id and dup.user_id and keep.user_id != dup.user_id:
            print(f"пропуск {keep_id}:{dup_id} — голоса двух разных учёток")
            continue
        if keep.user_id is None and dup.user_id is not None:
            keep.user_id = dup.user_id
        relinked = _absorb_profile(db, keep, dup)
        db.commit()
        print(f"{dup_id} → {keep_id} «{keep.display_name}»: перепривязано созвонов {relinked}")
    print("обновлено страниц созвонов:", _refresh_match_views_after_cleanup(db))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--merge", nargs="+", metavar="KEEP:DUP", help="слить выбранные пары (id оставляемого : id вливаемого)")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        if args.merge:
            merge(db, [tuple(int(x) for x in pair.split(":", 1)) for pair in args.merge])
            return 0
        pairs = candidate_pairs(db)
        path = write_report(pairs)
        print(f"пар на проверку: {len(pairs)} (похожий голос ≥ {int(VOICE_MIN * 100)} %: "
              f"{sum(1 for p in pairs if p['cos'] >= VOICE_MIN)}); отчёт: {path}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
