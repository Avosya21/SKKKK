"""Удалить дубликаты speaker_profiles (имена и похожие голоса).

  venv\\Scripts\\python.exe scripts\\dedupe_speaker_profiles.py
  venv\\Scripts\\python.exe scripts\\dedupe_speaker_profiles.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.app.db import SessionLocal
from app.app.models.speaker_profile import SpeakerProfile
from app.app.services.speaker_profiles import (
    cleanup_voice_profile_pool,
    speaker_names_are_aliases,
)
from asr.speaker_registry import cosine, unpack_embedding


def _preview(db) -> None:
    rows = db.query(SpeakerProfile).order_by(SpeakerProfile.display_name).all()
    print(f"profiles: {len(rows)}")
    by_exact: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        by_exact[row.display_name.strip().lower()].append(row.id)
    exact = [(n, ids) for n, ids in by_exact.items() if len(ids) > 1]
    print(f"exact name groups: {len(exact)}")
    for name, ids in sorted(exact, key=lambda x: (-len(x[1]), x[0])):
        print(f"  {name!r}: ids={ids}")
    print("alias name pairs:")
    for i, left in enumerate(rows):
        for right in rows[i + 1 :]:
            if speaker_names_are_aliases(left.display_name, right.display_name) and (
                left.display_name.strip().lower() != right.display_name.strip().lower()
            ):
                print(f"  {left.id}:{left.display_name!r} ~ {right.id}:{right.display_name!r}")
    print("high cosine (>= 0.95):")
    vecs = [(row, unpack_embedding(row.embedding)) for row in rows]
    for i, (left, lv) in enumerate(vecs):
        for right, rv in vecs[i + 1 :]:
            if len(lv) != len(rv) or not lv:
                continue
            score = cosine(lv, rv)
            if score >= 0.95:
                print(f"  {score:.3f}  {left.id}:{left.display_name!r} ~ {right.id}:{right.display_name!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge duplicate speaker_profiles")
    parser.add_argument("--dry-run", action="store_true", help="Only list duplicate groups")
    parser.add_argument("--no-views", action="store_true", help="Do not rebuild match/transcript HTML")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        _preview(db)
        if args.dry_run:
            return 0

        result = cleanup_voice_profile_pool(db, refresh_views=not args.no_views)
        after = db.query(SpeakerProfile).count()
        print(
            f"removed={result['removed']} exact={result['exact_removed']} "
            f"alias={result['alias_removed']} voice={result['voice_removed']} "
            f"orphans={result['orphans_removed']} views={result['views_refreshed']} "
            f"profiles after={after}"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
