"""Оркестратор: preflight → documents → audio → speakers → knowledge (+ optional cleanup)."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable


def run_step(name: str, script: str) -> int:
    print(f"\n=== {name}", flush=True)
    r = subprocess.run([PY, str(HERE / script)], cwd=str(HERE.parent.parent))
    print(f"=== {name} → {r.returncode}", flush=True)
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="Живой e2e материалов/знания на песочнице")
    ap.add_argument("--keep", action="store_true", help="не удалять загруженные артефакты")
    ap.add_argument("--skip-audio", action="store_true", help="только документы + knowledge")
    ap.add_argument("--skip-preflight", action="store_true")
    args = ap.parse_args()

    steps = []
    if not args.skip_preflight:
        steps.append(("preflight", "e2e_preflight.py"))
    steps.append(("documents", "e2e_documents.py"))
    if not args.skip_audio:
        steps.append(("audio", "e2e_audio.py"))
        steps.append(("speakers", "e2e_speakers.py"))
    steps.append(("knowledge", "e2e_knowledge.py"))

    for name, script in steps:
        code = run_step(name, script)
        if code != 0:
            return code

    if not args.keep:
        print("\n=== cleanup", flush=True)
        sys.path.insert(0, str(HERE))
        from common import ensure_login  # noqa: E402
        from e2e_audio import cleanup as cleanup_audio  # noqa: E402
        from e2e_documents import cleanup as cleanup_docs  # noqa: E402

        client, _ = ensure_login()
        cleanup_docs(client)
        if not args.skip_audio:
            cleanup_audio(client)
        print("cleanup done")
    else:
        print("KEEP: артефакты оставлены (см. e2e_state.json)")
    print("materials e2e OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
