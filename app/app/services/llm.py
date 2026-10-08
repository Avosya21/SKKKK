"""Единый адаптер LLM (LM Studio или Cursor API): вызовы без reasoning (`*_no_think`), разбор JSON, самопроверка обхода reasoning при старте."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from app.app.config import Settings
from app.app.cursor_compat import ensure_os_blocking_compat
from app.app.llm_settings import uses_local_gemma

ensure_os_blocking_compat()

logger = logging.getLogger("skkkk.llm")

_HOLD_WARNED: set[str] = set()


def _check_db_not_held(where: str) -> None:
    """LLM отвечает десятки секунд. Если в этот момент поток держит соединение БД (открытая транзакция после чтения),
    оно всё это время занято в пуле — при нескольких таких запросах пул исчерпывается и портал отвечает 500.
    Перед вызовом LLM транзакцию нужно закрыть (`db.commit()`)."""
    try:
        from app.app.db import thread_held_connections
    except Exception:  # noqa: BLE001
        return
    held = thread_held_connections()
    if not held:
        return
    message = f"LLM-вызов ({where}) при открытой транзакции БД: соединение занято на всё время ответа модели"
    if os.environ.get("SKKKK_STRICT_DB_HOLD") == "1":
        raise RuntimeError(message + "\n" + held[0])
    key = held[0][-400:]
    if key not in _HOLD_WARNED:
        _HOLD_WARNED.add(key)
        logger.warning("%s; где взято:\n%s", message, held[0])


def _no_open_db(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _check_db_not_held(fn.__name__)
        return fn(*args, **kwargs)

    return wrapper
ROOT = Path(__file__).resolve().parents[2]


def strip_json_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def provider_uses_lmstudio(settings: Settings) -> bool:
    return uses_local_gemma(settings.llm_provider)


# LM Studio /v1/chat/completions принимает reasoning_effort только из этого
# списка (проверено по валидации сервера LM Studio): иное значение даёт HTTP 400
# "Invalid 'reasoning_effort' value", а вовсе не молчаливое отбрасывание.
# Сервер сам маппит none->off, minimal->low, xhigh->high.
_LMSTUDIO_REASONING_LEVELS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})


def _lmstudio_chat_body(
    *,
    model: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str | None = None,
    enable_thinking: bool | None = None,
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
    if enable_thinking is not None:
        # Jinja-переменная шаблона Qwen: True -> модель думает перед ответом.
        body["chat_template_kwargs"] = {"enable_thinking": bool(enable_thinking)}
    if reasoning_effort is not None:
        level = reasoning_effort.strip().lower()
        if level in _LMSTUDIO_REASONING_LEVELS:
            body["reasoning_effort"] = level
        else:
            logger.warning(
                "lmstudio: reasoning_effort=%r не поддерживается (допустимо: %s); поле не отправлено",
                reasoning_effort,
                ", ".join(sorted(_LMSTUDIO_REASONING_LEVELS)),
            )
    return body


def _reasoning_params(
    settings: Settings,
    reasoning_effort: str | None,
    enable_thinking: bool | None,
) -> tuple[str | None, bool | None]:
    """Дефолты рассуждений из настроек; при выключенном мышлении усилие = none."""
    if enable_thinking is None:
        enable_thinking = settings.lmstudio_enable_thinking
    if reasoning_effort is None:
        reasoning_effort = settings.lmstudio_reasoning_effort
    if not enable_thinking:
        reasoning_effort = "none"
    return reasoning_effort, enable_thinking


@_no_open_db
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
    reasoning_effort: str | None = None,
    enable_thinking: bool | None = None,
) -> tuple[str, dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = _lmstudio_chat_body(
        model=model,
        system=system,
        user=user,
        temperature=temperature,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        enable_thinking=enable_thinking,
    )
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with _lane_request(system, user, max_tokens, "chat"), httpx.Client(timeout=timeout) as client:
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
        "lmstudio.chat ok model=%s chars=%s completion_tokens=%s reasoning_tokens=%s has_reasoning=%s",
        model,
        len(text),
        meta.get("completion_tokens"),
        meta.get("reasoning_tokens"),
        meta.get("had_reasoning_content"),
    )
    return text, meta


@_no_open_db
def lmstudio_completions_raw(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    timeout: float = 600,
) -> tuple[str, dict[str, Any]]:
    """Raw /v1/completions с вручную закрытым пустым <think></think>.

    Наш квант (swift-qwen3.8-27b-uncensored-dynamic-mtp) через /v1/chat/completions
    игнорирует и enable_thinking, и reasoning_effort — шаблон безусловно открывает
    <think> и тратит на него 10-15k токенов независимо от параметров (проверено:
    identical usage при enable_thinking=True/False и reasoning_effort=none/medium).
    Обход: собрать ChatML-промпт вручную и сразу передать закрытый пустой think-блок,
    тогда генерация идёт прямо в контент. На реальных map/reduce-промптах даёт
    ~6-9x меньше токенов и ~5-7x быстрее при том же качестве ответа.
    """
    with _lane_request(system, user, max_tokens, "completions"):
        return _completions_http(
            base_url=base_url, api_key=api_key, model=model, system=system, user=user,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout,
        )


def _completions_http(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> tuple[str, dict[str, Any]]:
    """Сам HTTP-вызов /v1/completions без полос: самопроверка сразу после загрузки и замер этапа 0."""
    prompt = (
        f"<|im_start|>system\n{system}<|im_end|>\n"
        f"<|im_start|>user\n{user}<|im_end|>\n"
        f"<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )
    url = f"{base_url.rstrip('/')}/completions"
    body = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "prompt": prompt,
        "stop": ["<|im_end|>", "<|im_start|>"],
    }
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
    text = (choices[0].get("text") or "").strip()
    usage = data.get("usage") or {}
    meta = {
        "reasoning_tokens": 0,
        "completion_tokens": usage.get("completion_tokens"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "had_reasoning_content": False,
    }
    logger.info(
        "lmstudio.completions_raw ok model=%s chars=%s completion_tokens=%s prompt_tokens=%s prompt_chars=%s",
        model,
        len(text),
        meta.get("completion_tokens"),
        meta.get("prompt_tokens"),
        len(system) + len(user),
    )
    return text, meta


def _lane_request(system: str, user: str, max_tokens: int, what: str):
    """Полоса модели (services/llm_lanes.py): загрузка по профилю, очередь, бюджет окна."""
    from app.app.services.llm_lanes import estimate_tokens, llm_request

    return llm_request(Settings(), est_tokens=estimate_tokens(system, user) + max_tokens, what=what)


NO_THINK_ENV = "LMSTUDIO_NO_THINK"
# Резерв на рассуждения, если пришлось откатиться на обычный чат-эндпоинт (пункт 2.11).
REASONING_FALLBACK_EXTRA_TOKENS = 8000
_no_think_state: dict[str, Any] = {"ok": None, "detail": "не проверялось"}


def no_think_enabled() -> bool:
    """Обход reasoning включён, если не выключен env LMSTUDIO_NO_THINK=0 и самопроверка его не забраковала."""
    if os.environ.get(NO_THINK_ENV, "1").strip().lower() in ("0", "false", "off"):
        return False
    return _no_think_state["ok"] is not False


def announce_llm_and_selfcheck(settings: Settings, log) -> None:
    """При старте процесса: записать активный провайдер/модель и проверить обход reasoning (в фоне)."""
    import threading

    from app.app.logging_setup import log_event

    log_event(
        log,
        "llm.active",
        provider=settings.llm_provider,
        model=settings.lmstudio_model if provider_uses_lmstudio(settings) else "cursor",
        no_think_env=os.environ.get(NO_THINK_ENV, "1"),
    )
    if os.environ.get("SKKKK_DISABLE_LLM_SELFCHECK") == "1":
        return  # тесты не должны будить настоящий LM Studio: он заново загрузит 18-гигабайтную модель в GPU
    if provider_uses_lmstudio(settings):
        # Только если модель уже в памяти: проверка не должна загружать 18 ГБ при старте процесса.
        # Иначе её запустит хранитель сразу после загрузки (llm_keeper._after_load).
        from app.app.services.llm_keeper import model_state

        if model_state(settings) == "loaded":
            threading.Thread(target=lmstudio_no_think_selfcheck, args=(settings,), daemon=True, name="no-think-selfcheck").start()


def no_think_status() -> dict[str, Any]:
    return {"enabled": no_think_enabled(), "selfcheck_ok": _no_think_state["ok"], "detail": _no_think_state["detail"]}


def lmstudio_no_think_selfcheck(settings: Settings) -> tuple[bool | None, str]:
    """Короткий вызов обхода: ответ непустой, без <think> и служебных токенов ChatML.

    True — обход работает; False — модель/шаблон другие, обход даёт мусор (переходим на chat-эндпоинт);
    None — LM Studio недоступна, вывода нет (обход не отключаем)."""
    try:
        # без полос и без загрузки: проверка идёт сразу после загрузки модели хранителем (llm_keeper._after_load)
        text, _meta = _completions_http(
            base_url=settings.lmstudio_base_url,
            api_key=settings.lmstudio_api_key,
            model=settings.lmstudio_model,
            system="Отвечай одним словом.",
            user="Ответь словом: готово",
            temperature=0.0,
            max_tokens=24,
            timeout=60,
        )
    except Exception as exc:
        _no_think_state.update(ok=None, detail=f"LM Studio недоступна для самопроверки: {exc}")
        logger.warning("lmstudio.no_think_selfcheck unavailable: %s", exc)
        return None, _no_think_state["detail"]
    lowered = text.lower()
    problems = []
    if not text.strip():
        problems.append("пустой ответ")
    if "<think" in lowered or "</think" in lowered:
        problems.append("в ответе есть <think>")
    if "<|im_" in lowered:
        problems.append("в ответе служебные токены ChatML")
    ok = not problems
    detail = "обход reasoning работает" if ok else "обход reasoning не подходит модели: " + ", ".join(problems)
    _no_think_state.update(ok=ok, detail=detail)
    (logger.info if ok else logger.warning)("lmstudio.no_think_selfcheck ok=%s %s model=%s", ok, detail, settings.lmstudio_model)
    return ok, detail


def chat_completion_no_think(
    settings: Settings,
    *,
    system: str,
    user: str,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    timeout: float = 600,
) -> str:
    """Как chat_completion, но для LM Studio обходит зависший reasoning (см. lmstudio_completions_raw).
    Не-LM Studio провайдеры (Cursor) идут через обычный chat_completion."""
    if not provider_uses_lmstudio(settings):
        return cursor_chat_raw(settings, system=system, user=user, timeout=timeout)
    if not no_think_enabled():
        return chat_completion(
            settings,
            system=system,
            user=user,
            temperature=temperature,
            max_tokens=max_tokens + REASONING_FALLBACK_EXTRA_TOKENS,
            timeout=timeout,
        )
    text, _meta = lmstudio_completions_raw(
        base_url=settings.lmstudio_base_url,
        api_key=settings.lmstudio_api_key,
        model=settings.lmstudio_model,
        system=system,
        user=user,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    return text


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


@_no_open_db
def cursor_chat_raw(
    settings: Settings,
    *,
    system: str,
    user: str,
    timeout: float = 600,
    image_data_url: str | None = None,
) -> str:
    import time

    from cursor_sdk import Agent, CursorAgentError

    if not settings.cursor_api_key.strip():
        raise RuntimeError("CURSOR_API_KEY не задан — добавьте ключ в .env")

    message = _cursor_user_message(system=system, user=user, image_data_url=image_data_url)
    options = _cursor_agent_options(settings)
    last_error: Exception | None = None
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            result = Agent.prompt(message, options)
        except CursorAgentError as exc:
            last_error = RuntimeError(f"Cursor API: {exc.message}")
            logger.warning(
                "cursor.chat attempt %s/%s CursorAgentError: %s",
                attempt,
                attempts,
                exc.message,
            )
            if attempt < attempts:
                time.sleep(5 * attempt)
                continue
            raise last_error from exc

        text = (result.result or "").strip()
        if result.status != "error" and text:
            logger.info(
                "cursor.chat ok run=%s model=%s duration_ms=%s chars=%s vision=%s attempt=%s",
                result.id,
                settings.cursor_model,
                result.duration_ms,
                len(text),
                bool(image_data_url),
                attempt,
            )
            if timeout and result.duration_ms and result.duration_ms > timeout * 1000:
                logger.warning("cursor.chat exceeded timeout budget (%ss)", timeout)
            return text

        detail = text or f"status={result.status} duration_ms={result.duration_ms}"
        last_error = RuntimeError(f"Cursor run failed ({result.id}): {detail[:500]}")
        logger.warning(
            "cursor.chat attempt %s/%s failed run=%s status=%s",
            attempt,
            attempts,
            result.id,
            result.status,
        )
        if attempt < attempts:
            time.sleep(5 * attempt)
            continue
        raise last_error

    raise last_error or RuntimeError("Cursor API returned empty result")


def chat_from_env(
    system: str,
    user: str,
    *,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    timeout: float = 600,
    reasoning_effort: str | None = None,
    enable_thinking: bool | None = None,
) -> tuple[str, dict[str, Any]]:
    """Для ASR-скриптов: Settings из .env + переменные окружения subprocess."""
    settings = Settings()
    if not provider_uses_lmstudio(settings):
        text = cursor_chat_raw(settings, system=system, user=user, timeout=timeout)
        return text, {}

    reasoning_effort, enable_thinking = _reasoning_params(
        settings, reasoning_effort, enable_thinking
    )
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
        enable_thinking=enable_thinking,
    )


def chat_from_env_no_think(
    system: str,
    user: str,
    *,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    timeout: float = 600,
) -> tuple[str, dict[str, Any]]:
    """chat_from_env, но для LM Studio обходит зависший reasoning (см. lmstudio_completions_raw).
    Для ASR-скриптов (asr/llm_client.py) — те же subprocess-настройки из .env."""
    settings = Settings()
    if not provider_uses_lmstudio(settings):
        text = cursor_chat_raw(settings, system=system, user=user, timeout=timeout)
        return text, {}
    if not no_think_enabled():
        return chat_from_env(
            system,
            user,
            temperature=temperature,
            max_tokens=max_tokens + REASONING_FALLBACK_EXTRA_TOKENS,
            timeout=timeout,
        )
    return lmstudio_completions_raw(
        base_url=settings.lmstudio_base_url,
        api_key=settings.lmstudio_api_key,
        model=settings.lmstudio_model,
        system=system,
        user=user,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def chat_completion(
    settings: Settings,
    *,
    system: str,
    user: str,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: float = 600,
    reasoning_effort: str | None = None,
    enable_thinking: bool | None = None,
) -> str:
    if not provider_uses_lmstudio(settings):
        return cursor_chat_raw(settings, system=system, user=user, timeout=timeout)
    reasoning_effort, enable_thinking = _reasoning_params(
        settings, reasoning_effort, enable_thinking
    )
    text, _meta = lmstudio_chat_raw(
        base_url=settings.lmstudio_base_url,
        api_key=settings.lmstudio_api_key,
        model=settings.lmstudio_model,
        system=system,
        user=user,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        reasoning_effort=reasoning_effort,
        enable_thinking=enable_thinking,
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
        # Явно гасим мышление: vision-ответы (OCR/JSON кадров) разбираются
        # парсером, а reasoning съел бы бюджет max_tokens (4096-6144).
        "chat_template_kwargs": {"enable_thinking": False},
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
    import time

    started = time.monotonic()
    # картинка занимает в окне модели порядка 1-2 тыс. токенов — учитываем с запасом
    with _lane_request(system, user_text, max_tokens + 2000, "vision"), httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"LM Studio HTTP {r.status_code} for {url}: {r.text.strip()[:500] or r.reason_phrase}")
        data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LM Studio returned empty choices")
    text = (choices[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    logger.info(
        "lmstudio.vision ok model=%s chars=%s completion_tokens=%s prompt_tokens=%s seconds=%.1f",
        settings.lmstudio_model,
        len(text),
        usage.get("completion_tokens"),
        usage.get("prompt_tokens"),
        time.monotonic() - started,
    )
    return text


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


def lmstudio_model_loaded(model: str, loaded_ids: list[str]) -> bool:
    base = model.split(":")[0]
    return any(mid == model or mid.startswith(base) for mid in loaded_ids)


def ensure_lmstudio_gemma_loaded(settings: Settings, *, timeout: float = 10) -> None:
    """LMSTUDIO_MODEL загружена строго по профилю (llm_keeper); если нет — загрузить. Для процессов ASR (asr/llm_client.py).

    Раньше проверка шла по /v1/models, а при автозагрузке LM Studio он перечисляет и не загруженные модели."""
    from app.app.services.llm_keeper import ensure_loaded

    ensure_loaded(settings, reason="ASR")


def check_llm_provider(settings: Settings):
    from app.app.preflight import CheckResult, FAIL, OK, WARN

    if provider_uses_lmstudio(settings):
        from app.app.services.llm_keeper import keeper_status

        status = keeper_status(settings)
        if status.get("state") is None:
            return CheckResult(
                "llm",
                FAIL,
                f"LM Studio {settings.lmstudio_base_url} недоступен",
                "запустите LM Studio и включите сервер",
            )
        if status.get("error"):
            return CheckResult("llm", FAIL, f"профиль модели: {status['error']}", "tools/lmstudio/qwen-profile.json")
        if status.get("jit"):
            return CheckResult(
                "llm",
                WARN,
                "в LM Studio включена автозагрузка моделей (JIT): модель может подняться не по профилю",
                "LM Studio → Developer → Server Settings: выключить Just-in-Time Model Loading",
            )
        if status.get("mismatch"):
            return CheckResult("llm", WARN, "модель загружена не по профилю: " + "; ".join(status["mismatch"]),
                               "перезагрузится по профилю при следующем запросе")
        state = "в памяти" if status.get("state") == "loaded" else "загрузится по требованию"
        return CheckResult("llm", OK, f"{settings.lmstudio_model} через LM Studio ({state})")

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
