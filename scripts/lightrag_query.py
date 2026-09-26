"""Запрос к LightRAG по карте SKKKK.

  venv\\Scripts\\python.exe scripts\\lightrag_query.py "какая кнопка вызывает speaker-recalculate"
  venv\\Scripts\\python.exe scripts\\lightrag_query.py "artifact_speaker_links" --mode global
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / "tools" / "lightrag" / ".env"
DEFAULT_URL = "http://127.0.0.1:9621"


def _load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip().strip("'\"")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Query SKKKK LightRAG knowledge graph")
    parser.add_argument("query")
    parser.add_argument("--mode", default="hybrid", choices=["local", "global", "hybrid", "naive", "mix"])
    parser.add_argument("--url", default=os.environ.get("LIGHTRAG_URL", DEFAULT_URL))
    parser.add_argument("--context-only", action="store_true", help="only_need_context")
    args = parser.parse_args()

    env = _load_env(ENV_PATH)
    api_key = env.get("LIGHTRAG_API_KEY", os.environ.get("LIGHTRAG_API_KEY", ""))
    base = args.url.rstrip("/")

    payload = {
        "query": args.query,
        "mode": args.mode,
        "only_need_context": args.context_only,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    req = urllib.request.Request(f"{base}/query", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        print(f"LightRAG недоступен: {exc}", file=sys.stderr)
        print("cd tools/lightrag && docker compose up -d", file=sys.stderr)
        return 1

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(raw)
        return 0

    if isinstance(data, dict):
        if "response" in data:
            print(data["response"])
        elif "answer" in data:
            print(data["answer"])
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
