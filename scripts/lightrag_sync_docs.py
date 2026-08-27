"""Скопировать документацию SKKKK в LightRAG inputs для /documents/scan.

  venv\\Scripts\\python.exe scripts\\lightrag_sync_docs.py
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIGHTRAG_ROOT = ROOT / "tools" / "lightrag"
ENV_PATH = LIGHTRAG_ROOT / ".env"


def _workspace_name() -> str:
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("WORKSPACE="):
                val = line.split("=", 1)[1].strip().strip("'\"")
                if val:
                    return val
    return "SKKKK_PROJECT_MAP"


INPUTS = LIGHTRAG_ROOT / "data" / "inputs" / _workspace_name()
MANIFEST = LIGHTRAG_ROOT / "data" / "inputs" / "_sync_manifest.sha256"

DOC_FILES = [
    ROOT / "AGENTS.md",
    ROOT / "docs" / "PROJECT-MAP.md",
    ROOT / "docs" / "PLAN.md",
    ROOT / "docs" / "DECISIONS.md",
    ROOT / "docs" / "TESTING.md",
    ROOT / "docs" / "project-map" / "manifest.yaml",
]

DOC_GLOBS = [
    (ROOT / "docs" / "stages", "*.md"),
    (ROOT / "docs" / "prompts", "*.md"),
    (ROOT / ".cursor" / "rules", "*.mdc"),
]

SOURCE_SUMMARIES = [
    (ROOT / "app" / "app" / "routers", "routers"),
    (ROOT / "app" / "app" / "services", "services"),
    (ROOT / "asr", "asr"),
]


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _write_module_index(base: Path, label: str, out_name: str) -> None:
    lines = [f"# SKKKK {label} — автогенерация для LightRAG", ""]
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts or path.name.startswith("test_"):
            continue
        rel = path.relative_to(ROOT).as_posix()
        doc = path.read_text(encoding="utf-8", errors="replace")
        first = ""
        for chunk in doc.split('"""'):
            if chunk.strip():
                first = chunk.strip().splitlines()[0][:200]
                break
        lines.append(f"## {rel}")
        if first:
            lines.append(first)
        lines.append("")
    out = INPUTS / "generated" / out_name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


def _fingerprint() -> str:
    h = hashlib.sha256()
    for path in sorted(INPUTS.rglob("*")):
        if path.is_file() and path.name != "_manifest.sha256":
            h.update(path.relative_to(INPUTS).as_posix().encode())
            h.update(path.read_bytes())
    return h.hexdigest()


def sync() -> dict[str, int]:
    if INPUTS.exists():
        shutil.rmtree(INPUTS)
    INPUTS.mkdir(parents=True)

    copied = 0
    for src in DOC_FILES:
        if not src.is_file():
            continue
        rel = src.relative_to(ROOT)
        _copy_file(src, INPUTS / rel)
        copied += 1

    for base, pattern in DOC_GLOBS:
        if not base.is_dir():
            continue
        for src in sorted(base.glob(pattern)):
            rel = src.relative_to(ROOT)
            _copy_file(src, INPUTS / rel)
            copied += 1

    for base, label in SOURCE_SUMMARIES:
        if base.is_dir():
            _write_module_index(base, label, f"{label}_index.md")
            copied += 1

    fp = _fingerprint()
    MANIFEST.write_text(fp + "\n", encoding="utf-8")
    return {"files": copied, "fingerprint": fp}


def main() -> int:
    stats = sync()
    print(f"LightRAG inputs: {stats['files']} items -> {INPUTS}")
    print(f"fingerprint: {stats['fingerprint'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
