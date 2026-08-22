from __future__ import annotations

import logging
from typing import Any

import httpx

from app.app.config import Settings
from app.app.services.ollama_runtime import ensure_ollama

logger = logging.getLogger("skkkk.ollama")
EMBED_DIM = 1024


def embed_texts(settings: Settings, texts: list[str], *, timeout: float = 120) -> list[list[float]]:
    if not texts:
        return []
    ensure_ollama(settings)
    url = f"{settings.ollama_base_url.rstrip('/')}/api/embed"
    body: dict[str, Any] = {
        "model": settings.ollama_embed_model,
        "input": texts,
        "options": {"num_gpu": settings.ollama_num_gpu},
    }
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=body)
        r.raise_for_status()
        data = r.json()
    vectors = data.get("embeddings") or []
    out: list[list[float]] = []
    for vec in vectors:
        if len(vec) != EMBED_DIM:
            raise ValueError(f"expected dim {EMBED_DIM}, got {len(vec)}")
        out.append(vec)
    return out


def ollama_tags(settings: Settings, timeout: float = 5) -> list[str]:
    ensure_ollama(settings, timeout=max(timeout, 30))
    url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
    with httpx.Client(timeout=timeout) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()
    models = data.get("models") or []
    return [m.get("name", "") for m in models if isinstance(m, dict)]
