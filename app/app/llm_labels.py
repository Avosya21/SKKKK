"""Подписи LLM для логов и UI в зависимости от провайдера (Gemma / Cursor)."""

from __future__ import annotations

from app.app.llm_settings import normalize_llm_provider, uses_local_gemma


def llm_short_name(provider: str | None) -> str:
    try:
        return "Cursor" if normalize_llm_provider(provider) == "cursor" else "Gemma"
    except ValueError:
        return "Gemma"


def llm_runtime_label(provider: str | None) -> str:
    name = llm_short_name(provider)
    if uses_local_gemma(provider):
        return f"{name} (LM Studio)"
    return f"{name} API"


def asr_step_labels(provider: str | None) -> dict[str, str]:
    name = llm_short_name(provider)
    if uses_local_gemma(provider):
        return {
            "audio_prep": "Подготовка аудио (FFmpeg)",
            "gemma_unload": "Выгрузка Gemma, освобождение GPU",
            "asr_containers": "Старт Docker: Whisper + GigaAM + T-one",
            "asr_run": "Три ASR параллельно (Whisper, T-one, GigaAM)",
            "asr_stop": "Остановка ASR-контейнеров, освобождение VRAM",
            "sortformer": "Sortformer: диаризация спикеров",
            "gemma_load": "Загрузка Gemma в LM Studio",
            "merge": "Склейка Gemma по окнам",
            "view_build": "Сборка HTML стенограммы",
        }
    return {
        "audio_prep": "Подготовка аудио (FFmpeg)",
        "gemma_unload": "Выгрузка LM Studio, GPU для ASR",
        "asr_containers": "Старт Docker: Whisper + GigaAM + T-one",
        "asr_run": "Три ASR параллельно (Whisper, T-one, GigaAM)",
        "asr_stop": "Остановка ASR-контейнеров, освобождение VRAM",
        "sortformer": "Sortformer: диаризация спикеров",
        "gemma_load": "LLM в облаке (Cursor), загрузка не нужна",
        "merge": f"Склейка {name} по окнам",
        "view_build": "Сборка HTML стенограммы",
    }


def knowledge_steps_audio(provider: str | None) -> list[tuple[str, str]]:
    name = llm_short_name(provider)
    return [
        ("entities", f"Извлечение сущностей ({name})"),
        ("index", "Индексация в sqlite-vec (bge-m3)"),
        ("summary", f"Выжимка созвона ({name})"),
        ("graph", "Сейв графа проекта"),
        ("finish", "Завершение"),
    ]


def knowledge_steps_doc(provider: str | None) -> list[tuple[str, str]]:
    name = llm_short_name(provider)
    return [
        ("entities", f"Извлечение сущностей ({name})"),
        ("index", "Индексация в sqlite-vec"),
        ("graph", "Сейв графа проекта"),
        ("finish", "Завершение"),
    ]


def llm_empty_response_message(provider: str | None) -> str:
    return f"{llm_short_name(provider)} вернула пустой ответ"


def llm_unavailable_hint(provider: str | None) -> str:
    if uses_local_gemma(provider):
        return "проверьте LM Studio / Gemma"
    return "проверьте CURSOR_API_KEY и интернет"
