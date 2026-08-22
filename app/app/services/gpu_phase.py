from __future__ import annotations

import logging
import subprocess
import time

logger = logging.getLogger("skkkk.gpu")


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
    subprocess.run(["lms", "unload", "--all"], check=False, timeout=120)


def lms_load_model(model: str) -> None:
    proc = subprocess.run(["lms", "load", model, "-y"], capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1000:]
        raise RuntimeError(f"lms load failed: {tail}")


def prepare_for_asr(vram_threshold_mib: int = 2500) -> int:
    lms_unload_all()
    return wait_vram_below(vram_threshold_mib)


def _model_loaded(model: str, loaded_ids: list[str]) -> bool:
    base = model.split(":")[0]
    for mid in loaded_ids:
        if mid == model or mid.startswith(base):
            return True
    return False


def prepare_for_llm(model: str, vram_threshold_mib: int = 2500, *, settings=None) -> None:
    if settings is not None:
        from app.app.llm_settings import uses_local_gemma

        if not uses_local_gemma(settings.llm_provider):
            logger.info("prepare_for_llm skipped: LLM_PROVIDER=cursor")
            return
    if settings is not None:
        try:
            from app.app.services.llm import lmstudio_models

            loaded = lmstudio_models(settings)
            if _model_loaded(model, loaded):
                logger.info("LLM already loaded: %s", model)
                return
        except Exception as exc:
            logger.warning("could not list LM Studio models: %s", exc)

    used = wait_vram_below(vram_threshold_mib)
    logger.info("loading LLM %s (vram before load %s MiB)", model, used)
    lms_load_model(model)
