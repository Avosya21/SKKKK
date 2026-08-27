"""Общая настройка onnxruntime и каталогов моделей для лаборатории.

Модели складываются в ``data/models/onnx-asr`` и ``data/models/onnx-diar``:
внутрь прогона их класть нельзя (сотни мегабайт на каждый прогон), а держать
в пользовательском кеше — значит потерять контроль над версией.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Hugging Face по умолчанию рвёт скачивание через 10 с — encoder-model.onnx.data ~2.4 ГБ.
if "HF_HUB_DOWNLOAD_TIMEOUT" not in os.environ:
    os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "600"

MODELS_DIR = ROOT / "data" / "models"
ASR_MODELS_DIR = MODELS_DIR / "onnx-asr"
DIAR_MODELS_DIR = MODELS_DIR / "onnx-diar"

CUDA_PROVIDERS = ["CUDAExecutionProvider", "CPUExecutionProvider"]
CPU_PROVIDERS = ["CPUExecutionProvider"]


class DeviceUnavailable(RuntimeError):
    """Запрошен CUDA, но onnxruntime его не видит. Молча уходить на CPU нельзя."""


def ensure_nvidia_dlls() -> list[str]:
    """cuDNN 9 из pip-пакета nvidia-cudnn-cu12 должен быть на PATH до InferenceSession.

    CUDA Toolkit 12.8 на этой машине есть, а cudnn64_9.dll в нём нет. Без этой
    DLL onnxruntime пишет CUDAExecutionProvider в список, затем падает и тихо
    считает граф на CPU.
    """
    added: list[str] = []
    site = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not site.is_dir():
        site = Path(sys.prefix) / "lib" / "site-packages" / "nvidia"
    if not site.is_dir():
        return added
    for package in site.iterdir():
        bindir = package / "bin"
        if not bindir.is_dir():
            continue
        path = str(bindir)
        added.append(path)
        add_dll = getattr(os, "add_dll_directory", None)
        if callable(add_dll):
            try:
                add_dll(path)
            except OSError:
                pass
        current = os.environ.get("PATH", "")
        if path.lower() not in current.lower().split(os.pathsep):
            os.environ["PATH"] = path + os.pathsep + current
    return added


def providers_for(device: str) -> list[str]:
    """Список провайдеров. ``cuda`` без CUDA — ошибка, а не тихий CPU.

    Тихий переход на CPU превращает двухминутный прогон в получасовой и
    объясняет «почему-то долго» вместо явного сообщения.
    """
    import onnxruntime as ort

    ensure_nvidia_dlls()
    available = set(ort.get_available_providers())
    if device == "cpu":
        return CPU_PROVIDERS
    if device == "cuda":
        if "CUDAExecutionProvider" not in available:
            raise DeviceUnavailable(
                "onnxruntime собран без CUDAExecutionProvider; доступны: "
                + ", ".join(sorted(available))
                + ". Поставьте onnxruntime-gpu или запустите с --device cpu."
            )
        return CUDA_PROVIDERS
    if device == "auto":
        return CUDA_PROVIDERS if "CUDAExecutionProvider" in available else CPU_PROVIDERS
    raise ValueError(f"неизвестное устройство {device!r}")


def model_cache(kind: str, name: str) -> Path:
    """Каталог под конкретную модель. Не создаём пустую папку: onnx-asr тогда
    считает, что файлы уже на месте, и не качает их с Hugging Face."""
    base = ASR_MODELS_DIR if kind == "asr" else DIAR_MODELS_DIR
    return base / name.replace("/", "__")


def runtime_info() -> dict[str, object]:
    import onnxruntime as ort

    return {
        "onnxruntime": ort.__version__,
        "providers": ort.get_available_providers(),
        "hf_offline": os.environ.get("HF_HUB_OFFLINE", "0"),
    }
