"""Выбор LLM-провайдера (Gemma / Cursor) — чтение и запись в `.env`."""

from __future__ import annotations

import re
from typing import Literal

from app.app.bootstrap import ENV_PATH, _read_env, _set

LlmProvider = Literal["lmstudio", "cursor"]
LLM_PROVIDERS: tuple[LlmProvider, ...] = ("lmstudio", "cursor")
DEFAULT_LLM_PROVIDER: LlmProvider = "lmstudio"
LLM_CLI_ALIASES: dict[str, LlmProvider] = {
    "gemma": "lmstudio",
    "lmstudio": "lmstudio",
    "cursor": "cursor",
}


def normalize_llm_provider(value: str | None) -> LlmProvider:
    raw = (value or DEFAULT_LLM_PROVIDER).strip().lower()
    if raw in LLM_CLI_ALIASES:
        return LLM_CLI_ALIASES[raw]
    if raw not in LLM_PROVIDERS:
        raise ValueError(f"LLM_PROVIDER must be one of {LLM_PROVIDERS}, got {raw!r}")
    return raw  # type: ignore[return-value]


def apply_llm_provider_to_process(provider: LlmProvider) -> None:
    """Синхронизировать выбранный провайдер с os.environ.

    Иначе системная LLM_PROVIDER перекрывает `.env` при чтении Settings.
    """
    import os

    os.environ["LLM_PROVIDER"] = normalize_llm_provider(provider)


def read_llm_provider_from_env_file() -> LlmProvider | None:
    if not ENV_PATH.is_file():
        return None
    raw = _get_env_value(_read_env(), "LLM_PROVIDER")
    if not raw:
        return None
    try:
        return normalize_llm_provider(raw)
    except ValueError:
        return None


def write_llm_provider_to_env(provider: LlmProvider) -> None:
    provider = normalize_llm_provider(provider)
    text = _read_env()
    updated = _set(text, "LLM_PROVIDER", provider)
    if updated != text:
        ENV_PATH.write_text(updated, encoding="utf-8")
    apply_llm_provider_to_process(provider)


def uses_local_gemma(provider: str | None) -> bool:
    try:
        return normalize_llm_provider(provider) == "lmstudio"
    except ValueError:
        return True


LLM_ENV_KEYS: tuple[str, ...] = (
    "LLM_PROVIDER",
    "LMSTUDIO_BASE_URL",
    "LMSTUDIO_MODEL",
    "LMSTUDIO_API_KEY",
    "CURSOR_API_KEY",
    "CURSOR_MODEL",
    "CURSOR_RUNTIME",
    "CURSOR_CLOUD_REPO",
)


def apply_llm_env_from_dotenv() -> None:
    """Подставить LLM-переменные из `.env` в os.environ (для standalone ASR-скриптов)."""
    import os

    if not ENV_PATH.is_file():
        return
    for key, value in _parse_env_pairs(_read_env()).items():
        if key in LLM_ENV_KEYS and value:
            os.environ.setdefault(key, value)


def _parse_env_pairs(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _get_env_value(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}\s*=(.*)$", text, flags=re.M)
    return match.group(1).strip() if match else ""
