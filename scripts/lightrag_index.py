"""Загрузить docs в LightRAG (POST /documents/text) и дождаться pipeline.

  venv\\Scripts\\python.exe scripts\\lightrag_index.py --upload
  venv\\Scripts\\python.exe scripts\\lightrag_index.py --upload --wait
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIGHTRAG_ROOT = ROOT / "tools" / "lightrag"
ENV_PATH = LIGHTRAG_ROOT / ".env"
DEFAULT_URL = "http://127.0.0.1:9621"
TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".mdc", ".txt"}


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


def _workspace_inputs(env: dict[str, str]) -> Path:
    ws = env.get("WORKSPACE", "SKKKK_PROJECT_MAP")
    return LIGHTRAG_ROOT / "data" / "inputs" / ws


def _request(method: str, url: str, *, api_key: str, body: bytes | None = None, timeout: float = 60) -> bytes:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _health(base: str, api_key: str) -> bool:
    try:
        _request("GET", f"{base}/health", api_key=api_key, timeout=5)
        return True
    except (urllib.error.URLError, TimeoutError):
        return False


def _pipeline_busy(base: str, api_key: str) -> bool:
    try:
        raw = _request("GET", f"{base}/documents/pipeline_status", api_key=api_key, timeout=10)
    except urllib.error.HTTPError:
        return True
    return b'"busy":true' in raw or b'"is_busy":true' in raw or b'"scanning":true' in raw


def _upload_inputs(base: str, api_key: str, inputs: Path) -> int:
    files = sorted(
        p
        for p in inputs.rglob("*")
        if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES and not p.name.startswith("_")
    )
    if not files:
        print(f"no text files in {inputs}", file=sys.stderr)
        return 0

    # Приоритет: карта проекта первой
    files.sort(key=lambda p: (0 if "PROJECT-MAP" in p.name else 1, str(p)))
    sent = 0
    for path in files:
        rel = path.relative_to(inputs).as_posix()
        payload = {
            "text": path.read_text(encoding="utf-8", errors="replace"),
            "file_source": rel,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            _request("POST", f"{base}/documents/text", api_key=api_key, body=body, timeout=120)
            sent += 1
            print(f"  queued: {rel}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            print(f"  FAIL {rel}: {detail[:200]}", file=sys.stderr)
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description="Index docs into LightRAG")
    parser.add_argument("--url", default=os.environ.get("LIGHTRAG_URL", DEFAULT_URL))
    parser.add_argument("--upload", action="store_true", help="POST /documents/text for each file")
    parser.add_argument("--wait", action="store_true", help="Poll pipeline until idle")
    parser.add_argument("--timeout", type=int, default=7200, help="Max wait seconds")
    args = parser.parse_args()

    env = _load_env(ENV_PATH)
    api_key = env.get("LIGHTRAG_API_KEY", os.environ.get("LIGHTRAG_API_KEY", ""))
    base = args.url.rstrip("/")
    inputs = _workspace_inputs(env)

    if not _health(base, api_key):
        print(f"LightRAG недоступен на {base}", file=sys.stderr)
        print("Запустите: cd tools/lightrag && docker compose up -d", file=sys.stderr)
        return 1

    if args.upload:
        print(f"Upload from {inputs} …")
        n = _upload_inputs(base, api_key, inputs)
        print(f"uploaded {n} files")
    else:
        print(f"POST {base}/documents/scan …")
        try:
            _request("POST", f"{base}/documents/scan", api_key=api_key, body=b"", timeout=120)
        except urllib.error.HTTPError as exc:
            print(f"scan failed: {exc.read().decode(errors='replace')}", file=sys.stderr)
            return 1

    if not args.wait:
        print("started (use --wait to block until pipeline idle)")
        return 0

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if not _pipeline_busy(base, api_key):
            print("pipeline idle — indexing done")
            return 0
        time.sleep(5)
        print("… indexing", flush=True)

    print("timeout waiting for pipeline", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
