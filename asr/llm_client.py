"""Единый LLM-клиент для ASR-скриптов (merge, summary)."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.app.config import Settings  # noqa: E402
from app.app.llm_labels import llm_runtime_label, llm_short_name  # noqa: E402
from app.app.llm_settings import uses_local_gemma  # noqa: E402
from app.app.services.llm import chat_from_env, ensure_lmstudio_gemma_loaded  # noqa: E402

logger = logging.getLogger("skkkk.llm.asr")


def _settings() -> Settings:
    return Settings()


def chat(
    system: str,
    user: str,
    *,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    timeout: float = 600,
) -> tuple[str, dict]:
    settings = _settings()
    provider = settings.llm_provider
    label = llm_runtime_label(provider)
    started = time.monotonic()
    logger.info(
        "llm.request provider=%s runtime=%s max_tokens=%s user_chars=%s",
        provider,
        label,
        max_tokens,
        len(user),
    )
    try:
        text, meta = chat_from_env(
            system,
            user,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
    except Exception:
        logger.exception("llm.error provider=%s runtime=%s", provider, label)
        raise
    elapsed = round(time.monotonic() - started, 1)
    logger.info(
        "llm.response provider=%s runtime=%s chars=%s seconds=%s",
        provider,
        label,
        len(text),
        elapsed,
    )
    return text, meta


def ensure_llm_ready() -> None:
    settings = _settings()
    provider = settings.llm_provider
    if not uses_local_gemma(provider):
        logger.info(
            "llm.ready provider=%s runtime=%s (local Gemma not required)",
            provider,
            llm_runtime_label(provider),
        )
        return
    logger.info("llm.check provider=%s model=%s", provider, settings.lmstudio_model)
    ensure_lmstudio_gemma_loaded(settings)
    logger.info("llm.ready provider=%s model=%s", provider, settings.lmstudio_model)


def llm_name() -> str:
    return llm_short_name(_settings().llm_provider)
