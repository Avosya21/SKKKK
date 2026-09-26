"""Фазы GPU: замер видеопамяти (nvidia-smi), ожидание освобождения, выгрузка и загрузка модели LM Studio, чтобы ASR и LLM по очереди делили одну карту."""

from __future__ import annotations

import logging
import re
import subprocess
import time

logger = logging.getLogger("skkkk.gpu")

# Пайплайн строго последовательный: между фазами модели выгружаются. Поэтому
# фиксированные пороги (3000/2500 MiB) ошибочны — они блокируют запуск ASR даже
# когда GPU просто держит рабочий контейнер gigaam (~3 GB) на карте 24 GB.
# Порог вычисляется от общего VRAM: ждём, пока освободится место под следующую модель.
DEFAULT_ASR_FALLBACK_MIB = 3000
ASR_MODELS_HEADROOM_MIB = 8000  # parakeet + sortformer/silero-vad + запас
DEFAULT_LLM_FALLBACK_MIB = 2500
LLM_KV_CONTEXT_MIB = 2048
LLM_QP_SIZEFACTOR = 0.6  # Q4-квантованная LLM ~0.6 GiB на млрд параметров


def nvidia_memory_total_mib() -> int:
    try:
        raw = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            text=True,
            timeout=15,
        ).strip()
        return int(raw.splitlines()[0].strip())
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        return -1


def nvidia_memory_used_mib() -> int:
    try:
        raw = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True,
            timeout=15,
        ).strip()
        return int(raw.splitlines()[0].strip())
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        return 0


def asr_vram_threshold_mib(total_mib: int) -> int:
    """Сколько VRAM можно считать 'достаточно свободно' перед запуском ONNX-ASR."""
    if total_mib <= 0:
        return DEFAULT_ASR_FALLBACK_MIB
    return max(DEFAULT_ASR_FALLBACK_MIB, total_mib - ASR_MODELS_HEADROOM_MIB)


def llm_vram_need_mib(model: str) -> int:
    """Оценка VRAM, которую займёт LLM (GGUF Q4 + KV cache), по имени модели."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b", model)
    if match:
        params_b = float(match.group(1))
        return int(params_b * LLM_QP_SIZEFACTOR * 1024) + LLM_KV_CONTEXT_MIB
    return LLM_KV_CONTEXT_MIB + 20_000


def llm_vram_threshold_mib(model: str, total_mib: int) -> int:
    """Сколько VRAM должно быть свободно (used <= threshold) для загрузки LLM."""
    if total_mib <= 0:
        return DEFAULT_LLM_FALLBACK_MIB
    return max(DEFAULT_LLM_FALLBACK_MIB, total_mib - llm_vram_need_mib(model))


def wait_vram_below(max_mib: int, timeout_sec: int = 120) -> int:
    deadline = time.time() + timeout_sec
    used = nvidia_memory_used_mib()
    while time.time() < deadline:
        used = nvidia_memory_used_mib()
        logger.info("vram %s MiB (target <= %s)", used, max_mib)
        if used <= max_mib:
            return used
        time.sleep(3)
    raise RuntimeError(f"VRAM still {used} MiB after {timeout_sec}s (need <= {max_mib})")


def lms_unload_all() -> None:
    try:
        subprocess.run(["lms", "unload", "--all"], check=False, timeout=120, capture_output=True)
    except Exception:
        pass


def lms_stop_process() -> None:
    """Принудительно остановить LM Studio (освобождает VRAM, если модель «приколота»
    активным клиентом и graceful unload не сработал)."""
    for img in ("LM Studio.exe", "lmstudio.exe", "LMStudio.exe"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", img],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except Exception:
            pass


def lms_load_model(model: str) -> None:
    proc = subprocess.run(["lms", "load", model, "-y"], capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1000:]
        raise RuntimeError(f"lms load failed: {tail}")


def prepare_for_asr(vram_threshold_mib: int | None = None) -> int:
    """Освободить VRAM перед ASR: graceful unload → при неудаче принудительная остановка.

    Порог берётся от общего VRAM карты (позволяет держать контейнер gigaam на GPU);
    явный ``vram_threshold_mib`` переопределяет авто-расчёт.
    """
    threshold = (
        vram_threshold_mib
        if vram_threshold_mib is not None
        else asr_vram_threshold_mib(nvidia_memory_total_mib())
    )
    lms_unload_all()
    try:
        return wait_vram_below(threshold, timeout_sec=60)
    except RuntimeError:
        logger.warning(
            "VRAM не освободился после lms unload — принудительно останавливаю LM Studio"
        )
        lms_stop_process()
        return wait_vram_below(threshold, timeout_sec=60)


def prepare_for_llm(model: str, vram_threshold_mib: int | None = None, *, settings=None) -> None:
    if settings is not None:
        from app.app.llm_settings import uses_local_gemma

        if not uses_local_gemma(settings.llm_provider):
            logger.info("prepare_for_llm skipped: LLM_PROVIDER=cursor")
            return
    if settings is not None:
        try:
            from app.app.services.llm import lmstudio_model_loaded, lmstudio_models

            loaded = lmstudio_models(settings)
            if lmstudio_model_loaded(model, loaded):
                logger.info("LLM already loaded: %s", model)
                return
        except Exception as exc:
            logger.warning("could not list LM Studio models: %s", exc)

    threshold = (
        vram_threshold_mib
        if vram_threshold_mib is not None
        else llm_vram_threshold_mib(model, nvidia_memory_total_mib())
    )
    used = wait_vram_below(threshold)
    logger.info("loading LLM %s (vram before load %s MiB)", model, used)
    lms_load_model(model)
