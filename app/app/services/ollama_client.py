"""Клиент Ollama: эмбеддинги (bge-m3, на CPU) и список моделей."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.app.config import Settings
from app.app.services.ollama_runtime import ensure_ollama

logger = logging.getLogger("skkkk.ollama")
EMBED_DIM = 1024
# Ollama падает 400, если передать сотни строк одним /api/embed (runner tokenize отваливается).
EMBED_BATCH_SIZE = 64


def _parse_embeddings(data: dict[str, Any]) -> list[list[float]]:
    vectors = data.get("embeddings") or []
    out: list[list[float]] = []
    for vec in vectors:
        if len(vec) != EMBED_DIM:
            raise ValueError(f"expected dim {EMBED_DIM}, got {len(vec)}")
        out.append(vec)
    return out


def embed_texts(settings: Settings, texts: list[str], *, timeout: float = 120) -> list[list[float]]:
    if not texts:
        return []
    ensure_ollama(settings)
    url = f"{settings.ollama_base_url.rstrip('/')}/api/embed"
    out: list[list[float]] = []
    with httpx.Client(timeout=timeout) as client:
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start : start + EMBED_BATCH_SIZE]
            body: dict[str, Any] = {
                "model": settings.ollama_embed_model,
                "input": batch,
                "options": {"num_gpu": settings.ollama_num_gpu},
            }
            r = client.post(url, json=body)
            if r.status_code >= 400:
                log_event = logger.error if r.status_code >= 500 else logger.warning
                log_event(
                    "ollama.embed failed batch=%s..%s status=%s body=%s",
                    start,
                    start + len(batch),
                    r.status_code,
                    (r.text or "")[:500],
                )
            r.raise_for_status()
            batch_vecs = _parse_embeddings(r.json())
            if len(batch_vecs) != len(batch):
                raise ValueError(
                    f"ollama returned {len(batch_vecs)} embeddings for batch of {len(batch)} texts"
                )
            out.extend(batch_vecs)
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
