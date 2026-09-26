"""Проверка LightRAG: Docker, Ollama embed, LLM, inputs.

  venv\\Scripts\\python.exe scripts\\lightrag_doctor.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / "tools" / "lightrag" / ".env"
DEFAULT_URL = "http://127.0.0.1:9621"


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _probe(url: str, path: str = "", timeout: float = 5) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}{path}", timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    env = _load_env()
    ws = env.get("WORKSPACE", "SKKKK_PROJECT_MAP")
    inputs = ROOT / "tools" / "lightrag" / "data" / "inputs" / ws
    ok = True

    print("=== LightRAG doctor ===")
    live, msg = _probe(DEFAULT_URL, "/health")
    print(f"LightRAG {DEFAULT_URL}: {'OK' if live else 'FAIL'} ({msg})")
    ok &= live

    n_inputs = len(list(inputs.rglob("*"))) if inputs.is_dir() else 0
    print(f"Inputs {inputs}: {n_inputs} paths")
    if n_inputs < 5:
        print("  -> run: venv\\Scripts\\python.exe scripts\\lightrag_sync_docs.py")
        ok = False

    embed_host = env.get("EMBEDDING_BINDING_HOST", "http://host.docker.internal:11434").replace(
        "host.docker.internal", "127.0.0.1"
    )
    elive, emsg = _probe(embed_host, "/api/tags")
    print(f"Ollama {embed_host}: {'OK' if elive else 'FAIL'} ({emsg})")

    llm_binding = env.get("LLM_BINDING", "openai")
    llm_host = env.get("LLM_BINDING_HOST", "http://host.docker.internal:1234/v1").replace(
        "host.docker.internal", "127.0.0.1"
    )
    if llm_binding == "ollama":
        llive, lmsg = _probe(llm_host.replace("/v1", ""), "/api/tags")
    else:
        llive, lmsg = _probe(llm_host, "/models")
    print(f"LLM ({llm_binding}) {llm_host}: {'OK' if llive else 'FAIL'} ({lmsg})")
    if not llive:
        print("  -> для индексации: запустите LM Studio (Gemma :1234) или Ollama chat-модель")
        ok = False

    api_key = env.get("LIGHTRAG_API_KEY", "")
    if live and api_key:
        try:
            req = urllib.request.Request(
                f"{DEFAULT_URL}/documents",
                headers={"X-API-Key": api_key},
            )
            raw = urllib.request.urlopen(req, timeout=10).read()
            statuses = json.loads(raw).get("statuses") or {}
            print(f"Indexed doc groups: {len(statuses)}")
        except urllib.error.URLError as exc:
            print(f"documents API: {exc}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
