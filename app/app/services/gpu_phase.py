"""Фазы GPU: замер видеопамяти (nvidia-smi), ожидание свободной памяти, подготовка ASR и этапа LLM. Загрузку и выгрузку модели делает хранитель (llm_keeper): на время GPU-шагов ASR модель выгружается (`asr_gpu_hold`)."""

from __future__ import annotations

import logging
import subprocess
import time

logger = logging.getLogger("skkkk.gpu")

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


def prepare_for_asr(vram_threshold_mib: int | None = None, *, settings=None) -> int:
    """Перед ASR: записать в журнал, что будет с моделью. Саму выгрузку на время GPU-шагов делает
    ``llm_keeper.asr_gpu_hold`` в ``pipeline_lab._run_cmd``: он дожидается текущих ответов (обе полосы), а прежняя
    выгрузка здесь обрывала ответ чата и при нехватке памяти останавливала LM Studio целиком.
    ``vram_threshold_mib`` оставлен для совместимости вызовов."""
    if settings is None:
        from app.app.config import get_settings

        settings = get_settings()
    used = nvidia_memory_used_mib()
    if settings.lmstudio_keep_during_asr:
        logger.info("ASR рядом с моделью: выгрузка не нужна (vram %s MiB)", used)
    else:
        logger.info("ASR: модель выгружается на время GPU-шагов (vram %s MiB)", used)
    return used


def prepare_for_llm(model: str, vram_threshold_mib: int | None = None, *, settings=None) -> None:
    """Модель загружена строго по профилю (llm_keeper). Сам вызов модели делает то же через полосу (llm_lanes);
    здесь — заранее, чтобы загрузка 18 ГБ не попадала во время первого запроса этапа."""
    if settings is None:
        from app.app.config import get_settings

        settings = get_settings()
    from app.app.services.llm_keeper import ensure_loaded

    ensure_loaded(settings, reason="этап LLM")
