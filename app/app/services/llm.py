from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

import httpx

from app.app.config import Settings
from app.app.cursor_compat import ensure_os_blocking_compat
from app.app.llm_settings import uses_local_gemma

ensure_os_blocking_compat()

logger = logging.getLogger("skkkk.llm")
ROOT = Path(__file__).resolve().parents[2]


def strip_json_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def provider_uses_lmstudio(settings: Settings) -> bool:
    return uses_local_gemma(settings.llm_provider)


def _lmstudio_chat_body(
    *,
    model: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str | None = "none",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
        body["chat_template_kwargs"] = {"enable_thinking": False}
    return body


def lmstudio_chat_raw(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    timeout: float = 600,
    reasoning_effort: str | None = "none",
) -> tuple[str, dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = _lmstudio_chat_body(
        model=model,
        system=system,
        user=user,
        temperature=temperature,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
    )
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, json=body, headers=headers)
        if response.status_code >= 400:
            detail = response.text.strip()[:500]
            raise RuntimeError(
                f"LM Studio HTTP {response.status_code} for {url}: {detail or response.reason_phrase}"
            )
        data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LM Studio returned empty choices")
    message = choices[0].get("message") or {}
    text = (message.get("content") or "").strip()
    usage = data.get("usage") or {}
    meta = {
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
        "completion_tokens": usage.get("completion_tokens"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "had_reasoning_content": bool(message.get("reasoning_content")),
    }
    logger.info(
        "lmstudio.chat ok model=%s chars=%s completion_tokens=%s",
        model,
        len(text),
        meta.get("completion_tokens"),
    )
    return text, meta


def _cursor_agent_options(settings: Settings):
    from cursor_sdk import AgentOptions, CloudAgentOptions, CloudRepository, LocalAgentOptions

    local = None
    cloud = None
    if settings.cursor_runtime == "cloud":
        repos = []
        if settings.cursor_cloud_repo.strip():
            repos.append(CloudRepository(url=settings.cursor_cloud_repo.strip()))
        cloud = CloudAgentOptions(repos=repos, skip_reviewer_request=True)
    else:
        local = LocalAgentOptions(cwd=str(ROOT), setting_sources=[])

    return AgentOptions(
        api_key=settings.cursor_api_key,
        model=settings.cursor_model,
        local=local,
        cloud=cloud,
    )


def _sdk_image_from_data_url(data_url: str):
    from cursor_sdk import SDKImage

    if not data_url.startswith("data:"):
        raise ValueError(f"ожидался data: URL, получено: {data_url[:48]!r}")
    header, payload = data_url.split(",", 1)
    mime = header[5:].split(";")[0].strip() or "image/jpeg"
    return SDKImage.from_data(payload, mime)


def _cursor_user_message(
    *,
    system: str,
    user: str,
    image_data_url: str | None = None,
):
    from cursor_sdk import UserMessage

    prompt = (
        f"{system.strip()}\n\n---\n\n{user.strip()}\n\n---\n\n"
        "Ответь только запрошенным текстом. Без пояснений, без markdown-обёрток, "
        "без вызова инструментов и без правок файлов."
    )
    images = None
    if image_data_url:
        images = (_sdk_image_from_data_url(image_data_url),)
    return UserMessage(text=prompt, images=images)


def cursor_chat_raw(
    settings: Settings,
    *,
    system: str,
    user: str,
    timeout: float = 600,
    image_data_url: str | None = None,
) -> str:
    from cursor_sdk import Agent, CursorAgentError

    if not settings.cursor_api_key.strip():
        raise RuntimeError("CURSOR_API_KEY не задан — добавьте ключ в .env")

    message = _cursor_user_message(system=system, user=user, image_data_url=image_data_url)
    options = _cursor_agent_options(settings)
    try:
        result = Agent.prompt(message, options)
    except CursorAgentError as exc:
        raise RuntimeError(f"Cursor API: {exc.message}") from exc

    if result.status == "error":
        raise RuntimeError(f"Cursor run failed: {result.id}")

    text = (result.result or "").strip()
    if not text:
        raise RuntimeError("Cursor API returned empty result")
    logger.info(
        "cursor.chat ok run=%s model=%s duration_ms=%s chars=%s vision=%s",
        result.id,
        settings.cursor_model,
        result.duration_ms,
        len(text),
        bool(image_data_url),
    )
    if timeout and result.duration_ms and result.duration_ms > timeout * 1000:
        logger.warning("cursor.chat exceeded timeout budget (%ss)", timeout)
    return text


def chat_from_env(
    system: str,
    user: str,
    *,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    timeout: float = 600,
    reasoning_effort: str | None = "none",
) -> tuple[str, dict[str, Any]]:
    """Для ASR-скриптов: Settings из .env + переменные окружения subprocess."""
    settings = Settings()
    if not provider_uses_lmstudio(settings):
        text = cursor_chat_raw(settings, system=system, user=user, timeout=timeout)
        return text, {}

    return lmstudio_chat_raw(
        base_url=settings.lmstudio_base_url,
        api_key=settings.lmstudio_api_key,
        model=settings.lmstudio_model,
        system=system,
        user=user,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        reasoning_effort=reasoning_effort,
    )


def chat_completion(
    settings: Settings,
    *,
    system: str,
    user: str,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: float = 600,
) -> str:
    if not provider_uses_lmstudio(settings):
        return cursor_chat_raw(settings, system=system, user=user, timeout=timeout)
    text, _meta = lmstudio_chat_raw(
        base_url=settings.lmstudio_base_url,
        api_key=settings.lmstudio_api_key,
        model=settings.lmstudio_model,
        system=system,
        user=user,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        reasoning_effort="none",
    )
    return text


def chat_completion_vision(
    settings: Settings,
    *,
    system: str,
    user_text: str,
    image_data_url: str,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    timeout: float = 600,
) -> str:
    """Multimodal: LM Studio (Gemma) или Cursor SDK (UserMessage + SDKImage)."""
    if not provider_uses_lmstudio(settings):
        return cursor_chat_raw(
            settings,
            system=system,
            user=user_text,
            image_data_url=image_data_url,
            timeout=timeout,
        )
    url = f"{settings.lmstudio_base_url.rstrip('/')}/chat/completions"
    body = {
        "model": settings.lmstudio_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            },
        ],
    }
    headers = {"Authorization": f"Bearer {settings.lmstudio_api_key}"}
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LM Studio returned empty choices")
    return (choices[0].get("message") or {}).get("content") or ""


def lmstudio_models(settings: Settings, timeout: float = 5) -> list[str]:
    url = f"{settings.lmstudio_base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {settings.lmstudio_api_key}"}
    with httpx.Client(timeout=timeout) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
    if isinstance(data, dict):
        data = data.get("data") or []
    return [row.get("id", "") for row in data if isinstance(row, dict)]


def ensure_lmstudio_gemma_loaded(settings: Settings, *, timeout: float = 10) -> None:
    """Проверка, что Gemma видна в LM Studio (для ASR merge/summary)."""
    models = urllib.request.urlopen(
        f"{settings.lmstudio_base_url.rstrip('/')}/models",
        timeout=timeout,
    ).read().decode("utf-8")
    if "gemma" not in models.lower():
        raise RuntimeError(
            "Gemma не в LM Studio. Сначала остановите GPU ASR-контейнеры и выполните: "
            f"lms load {settings.lmstudio_model} -y"
        )


def check_llm_provider(settings: Settings):
    from app.app.preflight import CheckResult, FAIL, OK, WARN

    if provider_uses_lmstudio(settings):
        try:
            models = lmstudio_models(settings)
        except Exception as exc:
            return CheckResult(
                "llm",
                FAIL,
                f"LM Studio {settings.lmstudio_base_url} недоступен: {exc}",
                "запустите LM Studio и включите сервер",
            )
        if settings.lmstudio_model not in models:
            return CheckResult(
                "llm",
                WARN,
                f"Gemma {settings.lmstudio_model} не загружена (доступно {len(models)})",
                "lms load " + settings.lmstudio_model,
            )
        return CheckResult("llm", OK, f"Gemma ({settings.lmstudio_model}) через LM Studio")

    if not settings.cursor_api_key.strip():
        return CheckResult(
            "llm",
            FAIL,
            "CURSOR_API_KEY не задан",
            "добавьте ключ в .env (Cursor Dashboard → Integrations)",
        )
    try:
        from cursor_sdk import Cursor

        models = Cursor.models.list(api_key=settings.cursor_api_key)
        ids = [getattr(row, "id", "") or str(row) for row in models]
    except Exception as exc:
        return CheckResult(
            "llm",
            FAIL,
            f"Cursor API недоступен: {exc}",
            "проверьте CURSOR_API_KEY и интернет",
        )
    wanted = settings.cursor_model
    if ids and wanted not in ids and not any(wanted in mid for mid in ids):
        return CheckResult(
            "llm",
            WARN,
            f"модель {wanted} не найдена в списке Cursor ({len(ids)} доступно)",
            "укажите CURSOR_MODEL из Cursor.models.list()",
        )
    runtime = settings.cursor_runtime
    detail = f"Cursor ({wanted}, runtime={runtime})"
    if runtime == "cloud" and not settings.cursor_cloud_repo.strip():
        return CheckResult(
            "llm",
            WARN,
            detail + ", CURSOR_CLOUD_REPO не задан",
            "для cloud укажите URL репозитория или поставьте CURSOR_RUNTIME=local",
        )
    return CheckResult("llm", OK, detail)


def parse_prompt_blocks(path_text: str) -> list[str]:
    blocks = path_text.split("```")
    return [b.strip() for i, b in enumerate(blocks) if i % 2 == 1 and b.strip()]
