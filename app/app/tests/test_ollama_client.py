from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from app.app.config import Settings
from app.app.services.ollama_client import EMBED_BATCH_SIZE, EMBED_DIM, embed_texts


def _settings() -> Settings:
    return Settings(ollama_base_url="http://127.0.0.1:11434", ollama_embed_model="bge-m3")


def _fake_response(count: int) -> httpx.Response:
    payload = {"embeddings": [[0.1] * EMBED_DIM for _ in range(count)]}
    return httpx.Response(200, json=payload, request=httpx.Request("POST", "http://127.0.0.1:11434/api/embed"))


def test_embed_texts_batches_large_input(monkeypatch):
    texts = [f"chunk-{i}" for i in range(EMBED_BATCH_SIZE + 10)]
    posted: list[int] = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            batch = json["input"]
            posted.append(len(batch))
            return _fake_response(len(batch))

    monkeypatch.setattr("app.app.services.ollama_client.ensure_ollama", lambda *a, **k: None)
    monkeypatch.setattr(httpx, "Client", FakeClient)

    vecs = embed_texts(_settings(), texts)
    assert len(vecs) == len(texts)
    assert posted == [EMBED_BATCH_SIZE, 10]


def test_embed_texts_empty():
    assert embed_texts(_settings(), []) == []


def test_embed_texts_dim_mismatch(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            return httpx.Response(
                200,
                json={"embeddings": [[0.1] * (EMBED_DIM - 1)]},
                request=httpx.Request("POST", "http://127.0.0.1:11434/api/embed"),
            )

    monkeypatch.setattr("app.app.services.ollama_client.ensure_ollama", lambda *a, **k: None)
    monkeypatch.setattr(httpx, "Client", FakeClient)

    with pytest.raises(ValueError, match="expected dim"):
        embed_texts(_settings(), ["one"])
