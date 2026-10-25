"""Хранитель локальной модели: загрузка строго по профилю `tools/lmstudio/qwen-profile.json`, сверка после загрузки,
выгрузка после простоя. Один межпроцессный замок на API, воркер и процессы ASR (план docs/plans/LLM-LANES-PLAN.md).

Автозагрузка LM Studio (JIT) поднимала модель с настройками по умолчанию (один поток, выгрузка через час), а `/v1/models`
при ней перечисляет все скачанные модели — проверка «загружена» всегда отвечала «да». Поэтому состояние читается из
списка `/api/v0/models` (`state`), а настройки загруженной модели — из `lms ps --json` (`contextLength`, `parallel`).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.app.config import Settings
from app.app.logging_setup import log_event

logger = logging.getLogger("primaryanalitics.llm_keeper")

ROOT = Path(__file__).resolve().parents[3]
PROFILE_PATH = ROOT / "tools" / "lmstudio" / "qwen-profile.json"
DISABLE_ENV = "PRIMARY_ANALITICS_DISABLE_LLM_KEEPER"  # тесты: не трогать настоящий LM Studio
MODEL_LOCK = "llm_model.lock"
LAST_USE_FILE = "llm_last_use"
# Загруженную и сверенную модель в этом процессе повторно через `lms ps` не сверяем чаще, чем раз в столько секунд.
VERIFY_CACHE_SEC = 60
# Оценка памяти под окно (KV q8_0, гибрид qwen35: полное внимание в каждом 4-м слое) — для ожидания VRAM перед загрузкой.
KV_MIB_PER_1K_TOKENS = 40
LOAD_OVERHEAD_MIB = 1024
VRAM_WAIT_BEFORE_LOAD_SEC = 30
LMS_LOAD_TIMEOUT_SEC = 600


class LlmLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class LmProfile:
    model_key: str
    context_length: int
    parallel: int
    gpu: str = "max"
    load_fields: list[dict[str, Any]] = field(default_factory=list)
    operation_fields: list[dict[str, Any]] = field(default_factory=list)


def load_profile(path: Path = PROFILE_PATH) -> LmProfile:
    data = json.loads(path.read_text(encoding="utf-8"))
    return LmProfile(
        model_key=str(data["model_key"]),
        context_length=int(data["context_length"]),
        parallel=int(data["parallel"]),
        gpu=str(data.get("gpu") or "max"),
        load_fields=list((data.get("load") or {}).get("fields") or []),
        operation_fields=list((data.get("operation") or {}).get("fields") or []),
    )


def lmstudio_home() -> Path:
    return Path(os.environ.get("LMSTUDIO_HOME") or (Path.home() / ".lmstudio"))


def lms_exe() -> str:
    found = shutil.which("lms")
    if found:
        return found
    fallback = lmstudio_home() / "bin" / "lms.exe"
    return str(fallback) if fallback.is_file() else "lms"


def _lms(args: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [lms_exe(), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _lms_json(args: list[str], *, timeout: float = 60) -> Any:
    proc = _lms(args, timeout=timeout)
    if proc.returncode != 0:
        raise LlmLoadError(f"lms {' '.join(args)} -> код {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}")
    return json.loads(proc.stdout or "null")


def downloaded_model(model_key: str) -> dict[str, Any] | None:
    for row in _lms_json(["ls", "--json"]) or []:
        if isinstance(row, dict) and row.get("modelKey") == model_key:
            return row
    return None


def loaded_entry(model_key: str) -> dict[str, Any] | None:
    for row in _lms_json(["ps", "--json"]) or []:
        if isinstance(row, dict) and model_key in (row.get("identifier"), row.get("modelKey")):
            return row
    return None


def model_config(profile: LmProfile) -> dict[str, Any]:
    """Конфиг модели LM Studio: поля профиля плюс окно и число потоков (их же передаём флагами `lms load`)."""
    skip = {"llm.load.contextLength", "llm.load.numParallelSessions"}
    load = [f for f in profile.load_fields if f.get("key") not in skip]
    load += [
        {"key": "llm.load.contextLength", "value": profile.context_length},
        {"key": "llm.load.numParallelSessions", "value": profile.parallel},
    ]
    return {"preset": "", "operation": {"fields": profile.operation_fields}, "load": {"fields": load}}


def model_config_path(indexed_id: str) -> Path:
    rel = Path(*indexed_id.replace("\\", "/").split("/"))
    return lmstudio_home() / ".internal" / "user-concrete-model-default-config" / (str(rel) + ".json")


def write_model_config(profile: LmProfile, indexed_id: str) -> Path:
    path = model_config_path(indexed_id)
    text = json.dumps(model_config(profile), ensure_ascii=False, indent=2)
    try:
        if path.read_text(encoding="utf-8") == text:
            return path
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def load_args(profile: LmProfile, *, ttl_sec: int) -> list[str]:
    args = [
        "load", profile.model_key,
        "--gpu", profile.gpu,
        "--context-length", str(profile.context_length),
        "--parallel", str(profile.parallel),
    ]
    if ttl_sec > 0:
        args += ["--ttl", str(ttl_sec)]
    return [*args, "-y"]


def profile_mismatch(profile: LmProfile, entry: dict[str, Any]) -> list[str]:
    problems = []
    if int(entry.get("contextLength") or 0) != profile.context_length:
        problems.append(f"окно {entry.get('contextLength')} вместо {profile.context_length}")
    if int(entry.get("parallel") or 0) != profile.parallel:
        problems.append(f"потоков {entry.get('parallel')} вместо {profile.parallel}")
    return problems


def _api_root(settings: Settings) -> str:
    base = settings.lmstudio_base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def model_state(settings: Settings, *, timeout: float = 5) -> str | None:
    """`loaded` / `not-loaded` по REST LM Studio; None — LM Studio не отвечает.

    Список `/api/v0/models`: при выключенной автозагрузке в нём только загруженные модели, а запрос одной незагруженной
    модели отвечает 400 «not found»; при включённой — все скачанные с полем `state`."""
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{_api_root(settings)}/api/v0/models")
        r.raise_for_status()
        rows = r.json().get("data") or []
    except Exception:  # noqa: BLE001
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("id") == settings.lmstudio_model:
            return str(row.get("state") or "loaded")
    return "not-loaded"


def jit_loading_enabled() -> bool | None:
    try:
        data = json.loads((lmstudio_home() / ".internal" / "http-server-config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return bool(data.get("justInTimeModelLoading"))


def vram_need_mib(profile: LmProfile, size_bytes: int) -> int:
    return int(size_bytes / 1024 / 1024) + profile.context_length * KV_MIB_PER_1K_TOKENS // 1000 + LOAD_OVERHEAD_MIB


# --- замок и учёт простоя -------------------------------------------------------------------------------------------

def _locks_dir(settings: Settings) -> Path:
    path = settings.data_dir / "locks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def model_lock(settings: Settings):
    from filelock import FileLock

    return FileLock(str(_locks_dir(settings) / MODEL_LOCK))


def touch_last_use(settings: Settings) -> None:
    try:
        (_locks_dir(settings) / LAST_USE_FILE).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def idle_seconds(settings: Settings) -> float:
    try:
        return max(0.0, time.time() - float((_locks_dir(settings) / LAST_USE_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return float("inf")


# --- загрузка и выгрузка --------------------------------------------------------------------------------------------

_verified: dict[str, float] = {}
_verified_guard = threading.Lock()


def _forget_verified() -> None:
    with _verified_guard:
        _verified.clear()


def _recently_verified(model: str) -> bool:
    with _verified_guard:
        return time.monotonic() - _verified.get(model, -1e9) < VERIFY_CACHE_SEC


def _mark_verified(model: str) -> None:
    with _verified_guard:
        _verified[model] = time.monotonic()


def _check_profile_matches_settings(settings: Settings, profile: LmProfile) -> None:
    if profile.model_key != settings.lmstudio_model:
        raise LlmLoadError(
            f"LMSTUDIO_MODEL={settings.lmstudio_model} не совпадает с профилем {PROFILE_PATH.name} "
            f"(model_key={profile.model_key})"
        )


def _wait_vram_for_load(profile: LmProfile, size_bytes: int) -> None:
    from app.app.services.gpu_phase import nvidia_memory_total_mib, wait_vram_below

    total = nvidia_memory_total_mib()
    if total <= 0:
        return
    threshold = max(0, total - vram_need_mib(profile, size_bytes))
    try:
        wait_vram_below(threshold, timeout_sec=VRAM_WAIT_BEFORE_LOAD_SEC)
    except RuntimeError as exc:
        log_event(logger, "llm.load.vram_busy", level=logging.WARNING, error=str(exc))


def _load_locked(settings: Settings, profile: LmProfile, *, reason: str) -> dict[str, Any]:
    info = downloaded_model(profile.model_key)
    if not info:
        raise LlmLoadError(f"модель {profile.model_key} не скачана в LM Studio")
    config_path = write_model_config(profile, str(info.get("indexedModelIdentifier") or ""))
    _wait_vram_for_load(profile, int(info.get("sizeBytes") or 0))
    started = time.monotonic()
    proc = _lms(load_args(profile, ttl_sec=settings.lmstudio_load_ttl_sec), timeout=LMS_LOAD_TIMEOUT_SEC)
    if proc.returncode != 0:
        raise LlmLoadError(f"lms load -> код {proc.returncode}: {(proc.stderr or proc.stdout)[-800:]}")
    entry = loaded_entry(profile.model_key)
    if not entry:
        raise LlmLoadError("lms load завершился, но модели нет в lms ps")
    problems = profile_mismatch(profile, entry)
    if problems:
        _lms(["unload", profile.model_key], timeout=120)
        raise LlmLoadError("модель загрузилась не по профилю: " + "; ".join(problems))
    log_event(
        logger,
        "llm.load",
        model=profile.model_key,
        reason=reason,
        context=entry.get("contextLength"),
        parallel=entry.get("parallel"),
        seconds=round(time.monotonic() - started, 1),
        config=config_path,
    )
    return entry


def ensure_loaded(settings: Settings, *, reason: str = "request") -> None:
    """Модель загружена и совпадает с профилем; иначе загрузить (под общим замком всех процессов)."""
    from app.app.llm_settings import uses_local_gemma

    if not uses_local_gemma(settings.llm_provider) or os.environ.get(DISABLE_ENV) == "1":
        return
    if _recently_verified(settings.lmstudio_model) and model_state(settings) == "loaded":
        return
    profile = load_profile()
    _check_profile_matches_settings(settings, profile)
    with model_lock(settings):
        entry = loaded_entry(profile.model_key)
        if entry:
            problems = profile_mismatch(profile, entry)
            if not problems:
                _mark_verified(profile.model_key)
                return
            log_event(logger, "llm.reload", level=logging.WARNING, model=profile.model_key, problems="; ".join(problems))
            _lms(["unload", profile.model_key], timeout=120)
        _forget_verified()
        _load_locked(settings, profile, reason=reason)
        _mark_verified(profile.model_key)
    touch_last_use(settings)
    _after_load(settings)


def _after_load(settings: Settings) -> None:
    """После загрузки — самопроверка обхода reasoning (при старте процессов её больше не делаем: она будила модель)."""
    try:
        from app.app.services.llm import lmstudio_no_think_selfcheck

        threading.Thread(target=lmstudio_no_think_selfcheck, args=(settings,), daemon=True, name="no-think-selfcheck").start()
    except Exception:  # noqa: BLE001
        logger.exception("no-think selfcheck start failed")


def unload(settings: Settings, *, reason: str) -> bool:
    """Выгрузить модель профиля. True — была загружена и выгружена."""
    with model_lock(settings):
        return _unload_locked(settings, reason=reason)


def _unload_locked(settings: Settings, *, reason: str) -> bool:
    _forget_verified()
    if model_state(settings) != "loaded":
        return False
    proc = _lms(["unload", settings.lmstudio_model], timeout=120)
    log_event(logger, "llm.unload", model=settings.lmstudio_model, reason=reason, ok=proc.returncode == 0)
    return proc.returncode == 0


def maybe_unload_idle(settings: Settings) -> bool:
    """Вызывает воркер, когда очередь пуста. Выгружает, если простой ≥ LMSTUDIO_IDLE_UNLOAD_SEC и обе полосы свободны."""
    from app.app.llm_settings import uses_local_gemma
    from app.app.services.llm_lanes import lanes_idle

    if not uses_local_gemma(settings.llm_provider) or settings.lmstudio_idle_unload_sec <= 0:
        return False
    if os.environ.get(DISABLE_ENV) == "1":
        return False
    if idle_seconds(settings) < settings.lmstudio_idle_unload_sec:
        return False
    if model_state(settings) != "loaded":
        return False
    with model_lock(settings):
        if idle_seconds(settings) < settings.lmstudio_idle_unload_sec or not lanes_idle(settings):
            return False
        return _unload_locked(settings, reason=f"простой {int(idle_seconds(settings))} с")


STATUS_CACHE_SEC = 15  # /health/ready опрашивается часто, а `lms ps` — отдельный процесс
_status_cache: dict[str, Any] = {"at": -1e9, "value": None}


HOLD_FILE = "asr_gpu.hold"
# Шаги ASR, которые занимают GPU (Parakeet + диаризация, GigaAM, WeSpeaker). Модели они не вызывают.
GPU_ASR_STEPS = frozenset({"lab_onnx", "lab_giga", "lab_voice"})


def asr_hold_active(settings: Settings) -> bool:
    return (_locks_dir(settings) / HOLD_FILE).is_file()


@contextmanager
def asr_gpu_hold(settings: Settings, *, step: str) -> Iterator[None]:
    """Р1 по замеру 2026-10-05: модель (22,4 ГБ) и ASR на GPU (до 6,5 ГБ) на карту 24 ГБ вместе не помещаются — Windows
    выносит память в общую, и распознавание с ответом чата встают на минуты. На время GPU-шага ASR: дождаться конца
    текущих ответов (обе полосы), выгрузить модель и не давать её загрузить, пока шаг идёт. Чат в это время ждёт
    в своей очереди; между шагами (вычитка, ответы) модель снова загружается по профилю.
    ``LMSTUDIO_KEEP_DURING_ASR=true`` — модель остаётся рядом с ASR (если карта позволяет)."""
    from app.app.llm_settings import uses_local_gemma

    if (
        step not in GPU_ASR_STEPS
        or settings.lmstudio_keep_during_asr
        or not uses_local_gemma(settings.llm_provider)
        or os.environ.get(DISABLE_ENV) == "1"
    ):
        yield
        return
    from app.app.services.llm_lanes import LANE_BACKGROUND, LANE_CHAT, _file_lock

    hold = _locks_dir(settings) / HOLD_FILE
    hold.write_text(f"{os.getpid()} {step}", encoding="utf-8")  # метка для чата: «идёт распознавание речи»
    started = time.monotonic()
    try:
        with ExitStack() as stack:
            # порядок как у запросов: полосы, затем замок модели — без взаимной блокировки
            stack.enter_context(_file_lock(settings, LANE_BACKGROUND))
            stack.enter_context(_file_lock(settings, LANE_CHAT))
            stack.enter_context(model_lock(settings))
            waited = round(time.monotonic() - started, 1)
            unloaded = _unload_locked(settings, reason=f"ASR на GPU: {step}")
            from app.app.services.gpu_phase import lms_unload_all

            lms_unload_all()  # и любые другие модели, загруженные в LM Studio вручную
            log_event(logger, "llm.asr_hold", step=step, waited=waited, unloaded=unloaded)
            yield
    finally:
        hold.unlink(missing_ok=True)
        log_event(logger, "llm.asr_hold.released", step=step, seconds=round(time.monotonic() - started, 1))


def keeper_status(settings: Settings) -> dict[str, Any]:
    """Для /health/ready и предстартовой проверки: состояние модели, совпадение с профилем, автозагрузка LM Studio."""
    if time.monotonic() - _status_cache["at"] < STATUS_CACHE_SEC and _status_cache["value"] is not None:
        return dict(_status_cache["value"])
    value = _keeper_status(settings)
    _status_cache.update(at=time.monotonic(), value=value)
    return dict(value)


def _keeper_status(settings: Settings) -> dict[str, Any]:
    out: dict[str, Any] = {"state": model_state(settings), "jit": jit_loading_enabled()}
    try:
        profile = load_profile()
        out["profile"] = {"context": profile.context_length, "parallel": profile.parallel}
        if out["state"] == "loaded":
            entry = loaded_entry(profile.model_key)
            out["mismatch"] = profile_mismatch(profile, entry) if entry else ["нет в lms ps"]
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:300]
    return out
