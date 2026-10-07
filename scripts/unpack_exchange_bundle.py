"""Разложить принятую посылку файлов агента в рабочую копию проекта (data/projects/<id>/config): карта, дипмапы, README.

  python scripts/unpack_exchange_bundle.py --project 18 --list
  python scripts/unpack_exchange_bundle.py --project 18 --bundle 1 --bundle 2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.app.config import get_settings  # noqa: E402
from app.app.db import SessionLocal  # noqa: E402
from app.app.services import exchange_files as xf  # noqa: E402
from app.app.services.exchange_unpack import unpack_bundle  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--bundle", type=int, action="append", default=[])
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        if a.list or not a.bundle:
            for b in xf.list_bundles(db, a.project):
                print(f"#{b['bundle_id']} {b['name']} [{b['status']}] файлов {b['file_count']}, байт {b['total_bytes']}")
            return 0
        for bid in a.bundle:
            print(json.dumps(unpack_bundle(db, get_settings(), a.project, bid), ensure_ascii=False))
        return 0
    except xf.FilesError as exc:
        print(f"ошибка ({exc.code}): {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
