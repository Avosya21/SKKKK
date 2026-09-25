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
    return {
        "audio_prep": "Подготовка WAV 16 кГц",
        "lab_onnx": "Parakeet + диаризация Nemotron",
        "lab_sense": f"Спорные спикеры ({name})",
        "lab_giga": "GigaAM — второй слух",
        "lab_reconcile": "Сверка lab vs GigaAM",
        "lab_proof": f"Вычитка текста ({name})",
        "lab_voice": "WeSpeaker — голоса",
        "speakers_confirm": "Подтверждение спикеров",
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
